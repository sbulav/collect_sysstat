#!/usr/bin/env python

import os
import time
import argparse
import sys
from pyrrd.rrd import DataSource, RRA, RRD
from pyrrd.graph import DEF, CDEF, VDEF, LINE, AREA, GPRINT
from pyrrd.graph import ColorAttributes, Graph
__version__ = 0.3
__author__ = "Sergey Bulavintsev"


# Initialize basic params
def initnamespace(namespace_args):
    # Modify parameters below
    rrdpath = '/tmp/test.rrd'          # Path to rrd database file
    interface_list = 'eth0'            # List of interfaces to obtain data
    block_dev_list = 'vda vdb vdb1'    # List of block dev to obtain data
    graphpath = '/var/www/dhcpflood/'  # Path to store graph files
    gwidth = 800                       # Width of output graphs
    gheight = 600                      # Height of output graphs
    gtime = 86400                       # Create graphs from gtime to NOW
    namedict = {}
    namedict['interface'] = interface_list
    namedict['disk'] = block_dev_list
    namedict['rrdpath'] = rrdpath
    namedict['graphpath'] = graphpath
    namedict['gwidth'] = gwidth
    namedict['gheight'] = gheight
    namedict['gtime'] = gtime
    namedict['cpu'] = True
    namedict['net'] = True
    namedict['block'] = True
    namedict['memory'] = True
    namedict['verbose'] = False        # Set to True or False for verbose
    if namespace_args.graph:
        namedict['graph'] = True
    else:
        namedict['graph'] = False
    return namedict


# Create command line Parser
def createParser():
    parser = argparse.ArgumentParser(description="""
Script to gather system network, memory, cpu and block devices stats.\n
All gathered data script can print or put into rrd database
Please set all required variables in script body
                                    """)
    parser.add_argument("-g", "--graph", action="store_true",
                        help="Create graph")
    return parser


# Read and parse cpu data from /proc/stat
def read_cpu_data():
    """Read data for all cpus from /proc/stat
    A dict is created for each cpu, mapping column names to values.
    See proc(5) for information about the columns in /proc/stat
    """
    STAT_COLUMNS = ['name', 'user', 'nice', 'system', 'idle',
                    'iowait', 'irq', 'softirq', 'steal',
                    'guest', 'guest-nice']
    cpus = {}
    with open('/proc/stat', 'r') as stat_fd:
        for line in stat_fd:
            if not line.startswith('cpu'):
                continue
            # Create a dict mapping the cpu column names to values.
            cpu = dict(zip(STAT_COLUMNS, line.strip().split()))
            cpus[cpu['name']] = cpu
    return cpus


# Calculate difference between cpu data
def diff_cpu_data(prev, cur, ticks_elapsed):
    """Calculate the different between two sets of cpu data.

    A new set with updated data is returned.
    """
    if not prev or not cur:
        return None
    diff_cpus = {}
    for cpu_name, prev_cpu in prev.iteritems():
        # If a cpu is not included in both sets, skip it.
        if cpu_name not in cur:
            continue
        cur_cpu = cur[cpu_name]
        diff_cpu = {'name': cpu_name}
        diff_cpus[cpu_name] = diff_cpu
        for column_name in prev_cpu:
            if column_name == 'name' or column_name not in cur_cpu:
                continue
            try:
                # This calculates the amount of time spent
                # doing a cpu usage type, in percent.
                # The diff value (cur-prev) is the amount of
                # ticks spent on this task since the last
                # reading, divided by the total amount of ticks
                # elapsed.
                diff_cpu[column_name] = float(int(cur_cpu[column_name]) - int(prev_cpu[column_name])) / ticks_elapsed * 100
            except ValueError:
                pass
    return diff_cpus


# Read and parse memory stats from /proc/meminfo
def readMemValues():
    ds_mem = ['MemFree', 'MemTotal', 'SwapFree', 'SwapTotal',
              'Active(anon)', 'Active(file)', 'Active', 'Inactive(anon)',
              'Inactive(file)', 'Inactive', 'Slab', 'Buffers', 'Cached',
              'Dirty', 'HugePages_Free', 'HugePages_Total', 'AnonHugePages',
              'AnonPages']
    memDict = {}
    with open('/proc/meminfo', 'r') as f:
        memory_data = f.readlines()
    for line in memory_data:
        x = line.split()
        key = x[0][:-1]
        data = int(x[1])
        if key in ds_mem:
            memDict[key] = data
    return memDict


# Read average cpu load for 1,5,15 min from /proc/loadavg
def readLoadAvgValues():
    with open('/proc/loadavg', 'r') as f:
        loadavg = f.readline()
    loadvalues = {"loadavg1min": loadavg.split()[0],
                  "loadavg5min": loadavg.split()[1],
                  "loadavg15min": loadavg.split()[2]}
    return loadvalues


# Count difference between cpu ticks
def readCpuValues():
    SLEEP_TIME = 2
    ticks_elapsed = os.sysconf(os.sysconf_names['SC_CLK_TCK']) * SLEEP_TIME
    cur_cpu_data = None
    cpu_data_diff = False
    while not cpu_data_diff:
        prev_cpu_data = cur_cpu_data
        cur_cpu_data = read_cpu_data()
        cpu_data_diff = diff_cpu_data(prev_cpu_data, cur_cpu_data, ticks_elapsed)
        if cpu_data_diff:
            return cpu_data_diff
        time.sleep(SLEEP_TIME)


# Read and parse network devices stats from /proc/net/dev
def readNetValues(ninterfaces):
    ninterfaces = ninterfaces.split(' ')
    with open('/proc/net/dev', 'r') as f:
        net_data = f.readlines()
    columnLine = net_data[1]
    _, receiveCols, transmitCols = columnLine.split("|")
    receiveCols = map(lambda a: "recv_"+a, receiveCols.split())
    transmitCols = map(lambda a: "trans_"+a, transmitCols.split())
    cols = receiveCols+transmitCols
    interfaces = {}
    for line in net_data[2:]:
        if line.find(":") < 0:
            continue
        interface, data = line.split(':')
        interfaceData = dict(zip(cols, data.split()))
        if ninterfaces:
            if any(interface.upper().lstrip() == ni.upper() for ni in ninterfaces):
                interfaces[interface.lstrip()] = interfaceData
        else:
            if 'lo' not in interface:
                interfaces[interface.lstrip()] = interfaceData
    return interfaces


# Read and parse block device data from /proc/diskstats
def readBlockValues(disks):
    file_path = '/proc/diskstats'
    result = {}
    dev = None
    disks = disks.split(' ')

    # ref: http://lxr.osuosl.org/source/Documentation/iostats.txt
    columns_disk = ['m', 'mm', 'dev', 'reads', 'rd_mrg', 'rd_sectors',
                    'ms_reading', 'writes', 'wr_mrg', 'wr_sectors',
                    'ms_writing', 'cur_ios', 'ms_doing_io', 'ms_weighted']

    columns_partition = ['m', 'mm', 'dev', 'reads', 'rd_sectors', 'writes', 'wr_sectors']

    lines = open(file_path, 'r').readlines()
    for line in lines:
        if line == '':
            continue
        split = line.split()
        if len(split) == len(columns_disk):
            columns = columns_disk
        elif len(split) == len(columns_partition):
            columns = columns_partition
        else:
            # No match
            continue

        data = dict(zip(columns, split))
        if dev is not None and dev != data['dev']:
            continue
        for key in data:
            if key != 'dev':
                data[key] = int(data[key])
        if disks:
            if any(data['dev'].upper() == nd.upper() for nd in disks):
                result[data['dev']] = data
        else:
            if 'loop' not in data['dev'] and 'ram' not in data['dev']:
                result[data['dev']] = data
    return result


# Print gathered block devices values
def printblockvalues(blockvalues):
    """ blockvalues is a dict
    {'vda1': {'ms_doing_io': 1625, 'ms_writing': 1074, 'wr_sectors': 88,
    'mm': 1, 'writes': 25, 'ms_weighted': 1719, 'm': 252, 'dev': 'vda1',
    'wr_mrg': 13, 'rd_mrg': 389, 'reads': 607, 'ms_reading': 646,
    'cur_ios': 0, 'rd_sectors': 4942}}
        """
    print("-----Collecting data on block devices-----------------")
    for devicename, devicedata in blockvalues.items():
        for block, data in devicedata.items():
            print "%s:%s:%s" % (devicename, block, data)


# Print gathered network devices values
def printnetvalues(netvalues):
    """ netvalues is a dict
    {'  eth0': {'recv_compressed': '0', 'recv_multicast': '0',
    'recv_bytes': '3729657901', 'trans_fifo': '0', 'recv_drop': '0',
    'recv_packets': '50089044', 'trans_compressed': '0', 'trans_drop': '0',
    'recv_fifo': '0', 'trans_bytes': '691562424', 'recv_errs': '0',
    'recv_frame': '0', 'trans_colls': '0', 'trans_carrier': '0',
    'trans_errs': '0', 'trans_packets': '710546'}}
    """
    print("-----Collecting data on network interfaces -------------")
    for interface, data in netvalues.items():
        print "Interface:%s" % interface.lstrip(),\
              "Received_bytes:%s, Trans_bytes:%s,\
 Received_packets:%s, Trans_packets:%s \
 Received_errors:%s, Trans_errors:%s" % \
             (data.get('recv_bytes'), data.get('trans_bytes'),
              data.get('recv_packets'), data.get('trans_packets'),
              data.get('recv_errs'), data.get('trans_errs'))


# Print gathered CPU values
def printcpuvalues(cpuvalues, loadavgvalues):
    """ loadavgvalues is dict
    {'loadavg1min': '0.00', 'loadavg15min': '0.00', 'loadavg5min': '0.00'}
    """
    """ cpu_data is a dict
    {'cpu': {'name': 'cpu', 'softirq': 0.0, 'iowait': 0.0, 'system': 0.0,
    'idle': 100.0, 'user': 0.0, 'irq': 0.0, 'nice': 0.0, 'steal': 0.0,
    'guest': 0.0}, 'cpu0': {'name': 'cpu0', 'softirq': 0.0, 'iowait': 0.0,
    'system': 0.0, 'idle': 100.0, 'user': 0.0, 'irq': 0.0, 'nice': 0.0,
    'steal': 0.0, 'guest': 0.0}}
     """
    cpus = ''
    for cpu in cpuvalues.itervalues():
        cpus += '%s, ' % cpu.get('name')
    print("-----Collecting data on all CPU:%s--------------------") % cpus
    print "Load_avg_1_min:%s, Load_avg_5min:%s, Load_avg_15min:%s" %\
        (loadavgvalues.get('loadavg1min'), loadavgvalues.get('loadavg5min'),
         loadavgvalues.get('loadavg15min'))
    for cpu in cpuvalues.itervalues():
        print "Cpu:%s, system:%s, user:%s, idle:%s, iowait:%s, irq:%s, softirq:%s,\
nice:%s, steal:%s, guest:%s" % (cpu.get('name'), cpu.get('system'),
                                cpu.get('user'), cpu.get('idle'),
                                cpu.get('iowait'), cpu.get('irq'),
                                cpu.get('softirq'), cpu.get('nice'),
                                cpu.get('steal'), cpu.get('guest'))


# Print gathered Memory values
def printmemvalues(memvalues):
    """ memvalues is a dict
    {'WritebackTmp': 0, 'SwapTotal': 1048568, 'Active(anon)': 7412,
    'SwapFree': 1037096, 'DirectMap4k': 8184, 'KernelStack': 1680,
    MemFree': 310716, 'HugePages_Rsvd': 0, 'Committed_AS': 125040,
    Active(file)': 348396, 'NFS_Unstable': 0, 'VmallocChunk': 34359718832,
    Writeback': 0, 'Inactive(file)': 195396, 'MemTotal': 1020180,
    VmallocUsed': 7332, 'HugePages_Free': 0, 'AnonHugePages': 2048,
    AnonPages': 18704, 'Active': 355808, 'Inactive(anon)': 12920,
    CommitLimit': 1558656, 'Hugepagesize': 2048, 'Cached': 323320,
    SwapCached': 1972, 'VmallocTotal': 34359738367, 'Shmem': 100,
    Mapped': 9488, 'SUnreclaim': 56876, 'Unevictable': 0,
    SReclaimable': 66480, 'Mlocked': 0, 'DirectMap2M': 1040384,
    HugePages_Surp': 0, 'Bounce': 0, 'Inactive': 208316,
    PageTables': 6492, 'HardwareCorrupted': 0, 'HugePages_Total': 0,
    Slab': 123356, 'Buffers': 220572, 'Dirty': 64}
    """
    print("-----Collecting data on memory -----------------------")
    for key, value in memvalues.items():
        print "%s:%s" % (key, value)


# Create list of DS based on cli options and gathered data
def createDSList(namespace, memvalues, netvalues, blockvalues,
                 cpuvalues, loadavgvalues):
    dataSources = []
    if namespace['memory']:
        for ds in memvalues:
            dataSource = DataSource(dsName=ds.replace('(', '_').strip(')'),
                                    dsType='GAUGE',
                                    heartbeat=180, minval=0)
            dataSources.append(dataSource)
    if namespace['cpu']:
        ds_loadavg = ['loadavg1min', 'loadavg5min', 'loadavg15min']
        ds_cpu = ['system', 'user', 'idle', 'iowait', 'irq', 'softirq',
                  'nice', 'steal', 'guest']
        for ds in ds_loadavg:
            dataSource = DataSource(dsName=ds, dsType='GAUGE',
                                    heartbeat=180, minval=0, maxval=100)
            dataSources.append(dataSource)
        for cpu in cpuvalues:
            for ds in ds_cpu:
                dataSource = DataSource(dsName=cpu+'_'+ds, dsType='GAUGE',
                                        heartbeat=180, minval=0)
                dataSources.append(dataSource)
    if namespace['block']:
        for blockdevice, blockdata in blockvalues.items():
            for ds in blockdata.keys():
                if ds != 'dev':
                    dataSource = DataSource(dsName=blockdevice+'_'+ds,
                                            dsType='GAUGE', heartbeat=180,
                                            minval=0)
                    dataSources.append(dataSource)
    if namespace['net']:
        ds_net = ['recv_bytes', 'trans_bytes', 'recv_packets', 'trans_packets',
                  'recv_errs', 'trans_errs']
        for interface in netvalues:
            for ds in ds_net:
                dataSource = DataSource(dsName=interface+'_'+ds,
                                        dsType='GAUGE', heartbeat=180,
                                        minval=0)
                dataSources.append(dataSource)
    return dataSources


# Create new RRA database
def createrra(namespace, memvalues, netvalues, blockvalues,
              cpuvalues, loadavgvalues):
    debug = False
    if namespace['verbose']:
        debug = True
        print "-----Creating new RRD database: %s ------------" %\
            namespace.get('rrdpath')
    dataSources = []
    dataSources = createDSList(namespace, memvalues, netvalues, blockvalues,
                               cpuvalues, loadavgvalues)
    roundRobinArchives = []
    roundRobinArchives.append(RRA(cf='AVERAGE', xff=0.5,
                                  steps=1, rows=10080))
    roundRobinArchives.append(RRA(cf='AVERAGE', xff=0.5,
                                  steps=3, rows=14400))
    roundRobinArchives.append(RRA(cf='AVERAGE', xff=0.5,
                                  steps=6, rows=22080))
    roundRobinArchives.append(RRA(cf='AVERAGE', xff=0.5,
                                  steps=10, rows=26352))
    roundRobinArchives.append(RRA(cf='MIN', xff=0.5,
                                  steps=1, rows=10080))
    roundRobinArchives.append(RRA(cf='MAX', xff=0.5,
                                  steps=1, rows=10080))
    roundRobinArchives.append(RRA(cf='LAST', xff=0.5,
                                  steps=1, rows=10080))
    if dataSources:
        myRRD = RRD(namespace.get('rrdpath'), ds=dataSources,
                    rra=roundRobinArchives, start=int(time.time()))
        myRRD.create(debug)
    else:
        print "ERROR: database %s not created:" % namespace.get('rrdpath')
        print "Please check parameters in the beginning"
        raise BaseException("Error: Script executed without input data")


# Create tempase and values strings to be used in RRD update
def createTemplateAndValues(memvalues, netvalues,
                            blockvalues, cpuvalues, loadavgvalues):
    values = ''
    templateds = ''
    if memvalues:
        for ds, value in memvalues.items():
            values += str(value) + ':'
            templateds += (ds.replace('(', '_').strip(')') + ':')
    if cpuvalues:
        for key, value in loadavgvalues.items():
            values += str(value) + ':'
            templateds += key + ':'
        for cpu in cpuvalues.items():
            for key, value in cpu[1].items():
                if key != 'name' and key != 'guest-nice':
                    values += str(value) + ':'
                    templateds += cpu[0] + '_' + key + ':'
    if netvalues:
        for interface in netvalues.items():
            for key, value in interface[1].items():
                if key in ['recv_bytes', 'trans_bytes', 'recv_packets',
                           'trans_packets', 'recv_errs', 'trans_errs']:
                    values += str(value) + ':'
                    templateds += interface[0] + '_' + key + ':'
    if blockvalues:
        for blockdevice in blockvalues.items():
            for key, value in blockdevice[1].items():
                if key != 'dev':
                    values += str(value) + ':'
                    templateds += blockdevice[0] + '_' + key + ':'
    return values, templateds


# Update existing RRA based on DS list
def updaterra(namespace, memvalues, netvalues,
              blockvalues, cpuvalues, loadavgvalues):
    values, templateds = createTemplateAndValues(memvalues,
                                                 netvalues, blockvalues,
                                                 cpuvalues, loadavgvalues)
    debug = False
    if namespace['verbose']:
        print "-----Database file exists  ---------"
        print "-----Updatine existing RRD database: %s ---------" %\
            namespace.get('rrdpath')
        debug = namespace['verbose']
    now = int(time.time())
    now += 1
    myRRD = RRD(namespace.get('rrdpath'))
    myRRD.bufferValue(now, values[:-1])
    try:
        myRRD.update(debug, dryRun=False, template=templateds[:-1])
    except:
        print "----------------------------------------"
        print "Error: update existing RRD failed"
        print "Please check that RRD contains valid DS list"
        print "You can remove existing RRD and create new one with correct DS"


def draw_file(namespace, memvalues, netvalues, blockvalues,
              cpuvalues, loadavgvalues):
    gtime = namespace.get('gtime')
    ca = ColorAttributes()
    ca.back = '#333333'
    ca.canvas = '#333333'
    ca.shadea = '#000000'
    ca.shadeb = '#111111'
    ca.mgrid = '#CCCCCC'
    ca.axis = '#FFFFFF'
    ca.frame = '#AAAAAA'
    ca.font = '#FFFFFF'
    ca.arrow = '#FFFFFF'
    """
    colors = ['#ff0000', '#ff4000', '#ff8000', '#ffbf00', '#ffff00', '#bfff00',
              '#80ff00', '#40ff00', '#00ff00', '#00ff40', '#00ff80', '#00ffbf',
              '#00ffff', '#00bfff', '#0080ff', '#0040ff', '#0000ff', '#4000ff',
              '#8000ff', '#bf00ff', '#ff00ff', '#ff00bf', '#ff0080', '#ff0040',
              '#ff0000']
    lines = []
    """
    if memvalues:
        ##########################
        # Memory summary
        ##########################
        def1 = DEF(rrdfile=namespace.get('rrdpath'), vname='Buffers',
                   dsName='Buffers')
        def2 = DEF(rrdfile=namespace.get('rrdpath'), vname='Cached',
                   dsName='Cached')
        def3 = DEF(rrdfile=namespace.get('rrdpath'), vname='Slab',
                   dsName='Slab')
        def4 = DEF(rrdfile=namespace.get('rrdpath'), vname='MemFree',
                   dsName='MemFree')
        def5 = DEF(rrdfile=namespace.get('rrdpath'), vname='MemTotal',
                   dsName='MemTotal')

        cdef1 = CDEF(vname='buffers_c', rpn='%s,1024,*' % def1.vname)
        cdef2 = CDEF(vname='cached_c', rpn='%s,1024,*' % def2.vname)
        cdef3 = CDEF(vname='slab_c', rpn='%s,1024,*' % def3.vname)
        cdef4 = CDEF(vname='memfree_c', rpn='%s,1024,*' % def4.vname)
        cdef5 = CDEF(vname='memtotal_c', rpn='%s,1024,*' % def5.vname)
        cdef6 = CDEF(vname='mem_used',
                     rpn='memtotal_c,memfree_c,-,slab_c,-,cached_c,-,buffers_c,-')

        vdef1 = VDEF(vname='buffers_last', rpn='%s,LAST' % cdef1.vname)
        vdef2 = VDEF(vname='cached_last', rpn='%s,LAST' % cdef2.vname)
        vdef3 = VDEF(vname='slab_last', rpn='%s,LAST' % cdef3.vname)
        vdef4 = VDEF(vname='memfree_last', rpn='%s,LAST' % cdef4.vname)
        vdef6 = VDEF(vname='used_last', rpn='%s,LAST' % cdef6.vname)

        vdef11 = VDEF(vname='buffers_avg', rpn='%s,AVERAGE' % cdef1.vname)
        vdef12 = VDEF(vname='cached_avg', rpn='%s,AVERAGE' % cdef2.vname)
        vdef13 = VDEF(vname='slab_avg', rpn='%s,AVERAGE' % cdef3.vname)
        vdef14 = VDEF(vname='memfree_avg', rpn='%s,AVERAGE' % cdef4.vname)
        vdef16 = VDEF(vname='used_avgg', rpn='%s,AVERAGE' % cdef6.vname)

        vdef21 = VDEF(vname='buffers_min', rpn='%s,MINIMUM' % cdef1.vname)
        vdef22 = VDEF(vname='cached_min', rpn='%s,MINIMUM' % cdef2.vname)
        vdef23 = VDEF(vname='slab_min', rpn='%s,MINIMUM' % cdef3.vname)
        vdef24 = VDEF(vname='memfree_min', rpn='%s,MINIMUM' % cdef4.vname)
        vdef26 = VDEF(vname='used_min', rpn='%s,MINIMUM' % cdef6.vname)

        vdef31 = VDEF(vname='buffers_max', rpn='%s,MAXIMUM' % cdef1.vname)
        vdef32 = VDEF(vname='cached_max', rpn='%s,MAXIMUM' % cdef2.vname)
        vdef33 = VDEF(vname='slab_max', rpn='%s,MAXIMUM' % cdef3.vname)
        vdef34 = VDEF(vname='memfree_max', rpn='%s,MAXIMUM' % cdef4.vname)
        vdef35 = VDEF(vname='memtotal_max', rpn='%s,MAXIMUM' % cdef5.vname)
        vdef36 = VDEF(vname='used_max', rpn='%s,MAXIMUM' % cdef5.vname)

        area1 = AREA(defObj=cdef1, color='#FFF200FF', legend='Buffers',
                     stack=True)
        gprint1 = GPRINT(vdef1, 'LAST:%8.2lf%s')
        gprint11 = GPRINT(vdef11, 'AVG:%8.2lf%s')
        gprint21 = GPRINT(vdef21, 'MIN:%8.2lf%s')
        gprint31 = GPRINT(vdef31, 'MAX:%8.2lf%s\l')

        area2 = AREA(defObj=cdef2, color='#6EA100FF', legend='Cached',
                     stack=True)
        gprint2 = GPRINT(vdef2, 'LAST:%8.2lf%s')
        gprint12 = GPRINT(vdef12, 'AVG:%8.2lf%s')
        gprint22 = GPRINT(vdef22, 'MIN:%8.2lf%s')
        gprint32 = GPRINT(vdef32, 'MAX:%8.2lf%s\l')

        area3 = AREA(defObj=cdef3, color='#1EA100FF', legend='Slab',
                     stack=True)
        gprint3 = GPRINT(vdef3, 'LAST:%8.2lf%s')
        gprint13 = GPRINT(vdef13, 'AVG:%8.2lf%s')
        gprint23 = GPRINT(vdef23, 'MIN:%8.2lf%s')
        gprint33 = GPRINT(vdef33, 'MAX:%8.2lf%s\l')

        area4 = AREA(defObj=cdef4, color='#12B3B5FF', legend='MemFree',
                     stack=True)
        gprint4 = GPRINT(vdef4, 'LAST:%8.2lf%s')
        gprint14 = GPRINT(vdef14, 'AVG:%8.2lf%s')
        gprint24 = GPRINT(vdef24, 'MIN:%8.2lf%s')
        gprint34 = GPRINT(vdef34, 'MAX:%8.2lf%s\l')

        area6 = AREA(defObj=cdef6, color='#ff0000', legend='Processes')
        gprint6 = GPRINT(vdef6, 'LAST:%8.2lf%s')
        gprint16 = GPRINT(vdef16, 'AVG:%8.2lf%s')
        gprint26 = GPRINT(vdef26, 'MIN:%8.2lf%s')
        gprint36 = GPRINT(vdef36, 'MAX:%8.2lf%s\l')

        line5 = LINE(defObj=cdef5, color='#FFFFFFFF', legend='MemTotal')
        gprint5 = GPRINT(vdef35, 'MAX:%8.2lf%s\l')
        paramlist = [def1, cdef1, vdef1, vdef11, vdef21, vdef31,
                     def2, cdef2, vdef2, vdef12, vdef22, vdef32,
                     def3, cdef3, vdef3, vdef13, vdef23, vdef33,
                     def4, cdef4, vdef4, vdef14, vdef24, vdef34,
                     def5, cdef5, vdef35,
                     cdef6, vdef6, vdef16, vdef26, vdef36,
                     area6, gprint6, gprint16, gprint26, gprint36,
                     area1, gprint1, gprint11, gprint21, gprint31,
                     area2, gprint2, gprint12, gprint22, gprint32,
                     area3, gprint3, gprint13, gprint23, gprint33,
                     area4, gprint4, gprint14, gprint24, gprint34,
                     line5, gprint5]
        g = Graph(namespace.get('graphpath')+'memory_summary.png',
                  start=(int(time.time())-gtime),
                  end=int(time.time()),
                  vertical_label='Memory_usage', color=ca)
        g.data.extend(paramlist)
        g.title = "Memory_utilization"
        g.width = namespace.get('gwidth')
        g.height = namespace.get('gheight')
        g.write()
        # #########################
        # Memory active
        # #########################
        def1 = DEF(rrdfile=namespace.get('rrdpath'), vname='Active_anon',
                   dsName='Active_anon')
        def2 = DEF(rrdfile=namespace.get('rrdpath'), vname='Active_file',
                   dsName='Active_file')
        def3 = DEF(rrdfile=namespace.get('rrdpath'), vname='Active',
                   dsName='Active')
        def4 = DEF(rrdfile=namespace.get('rrdpath'), vname='Inactive_anon',
                   dsName='Inactive_anon')
        def5 = DEF(rrdfile=namespace.get('rrdpath'), vname='Inactive_file',
                   dsName='Inactive_file')
        def6 = DEF(rrdfile=namespace.get('rrdpath'), vname='Inactive',
                   dsName='Inactive')
        cdef1 = CDEF(vname='active_anon_c', rpn='%s,1024,*' % def1.vname)
        cdef2 = CDEF(vname='active_file_c', rpn='%s,1024,*' % def2.vname)
        cdef3 = CDEF(vname='active_c', rpn='%s,1024,*' % def3.vname)
        cdef4 = CDEF(vname='inactive_anon_c', rpn='%s,1024,*' % def4.vname)
        cdef5 = CDEF(vname='inactive_file_c', rpn='%s,1024,*' % def5.vname)
        cdef6 = CDEF(vname='inactive_c', rpn='%s,1024,*' % def6.vname)

        vdef1 = VDEF(vname='active_anon_last', rpn='%s,LAST' % cdef1.vname)
        vdef2 = VDEF(vname='active_file_last', rpn='%s,LAST' % cdef2.vname)
        vdef4 = VDEF(vname='inactive_anon_last', rpn='%s,LAST' % cdef4.vname)
        vdef5 = VDEF(vname='inactive_file_last', rpn='%s,LAST' % cdef5.vname)

        vdef11 = VDEF(vname='active_anon_avg', rpn='%s,AVERAGE' % cdef1.vname)
        vdef12 = VDEF(vname='active_file_avg', rpn='%s,AVERAGE' % cdef2.vname)
        vdef14 = VDEF(vname='inactive_anon_avg', rpn='%s,AVERAGE' % cdef4.vname)
        vdef15 = VDEF(vname='inactive_file_avg', rpn='%s,AVERAGE' % cdef5.vname)

        vdef21 = VDEF(vname='active_anon_min', rpn='%s,MINIMUM' % cdef1.vname)
        vdef22 = VDEF(vname='active_file_min', rpn='%s,MINIMUM' % cdef2.vname)
        vdef24 = VDEF(vname='inactive_anon_min', rpn='%s,MINIMUM' % cdef4.vname)
        vdef25 = VDEF(vname='inactive_file_min', rpn='%s,MINIMUM' % cdef5.vname)

        vdef31 = VDEF(vname='active_anon_max', rpn='%s,MAXIMUM' % cdef1.vname)
        vdef32 = VDEF(vname='active_file_max', rpn='%s,MAXIMUM' % cdef2.vname)
        vdef33 = VDEF(vname='active_max', rpn='%s,MAXIMUM' % cdef3.vname)
        vdef34 = VDEF(vname='inactive_anon_max', rpn='%s,MAXIMUM' % cdef4.vname)
        vdef35 = VDEF(vname='inactive_file_max', rpn='%s,MAXIMUM' % cdef5.vname)
        vdef36 = VDEF(vname='inactive_max', rpn='%s,MAXIMUM' % cdef6.vname)

        area1 = AREA(defObj=cdef1, color='#006600', legend='Active_anon')
        gprint1 = GPRINT(vdef1, 'LAST:%8.2lf%s')
        gprint11 = GPRINT(vdef11, 'AVG:%8.2lf%s')
        gprint21 = GPRINT(vdef21, 'MIN:%8.2lf%s')
        gprint31 = GPRINT(vdef31, 'MAX:%8.2lf%s\l')

        area2 = AREA(defObj=cdef2, color='#00cc99', legend='Active_file',
                     stack=True)
        gprint2 = GPRINT(vdef2, 'LAST:%8.2lf%s')
        gprint12 = GPRINT(vdef12, 'AVG:%8.2lf%s')
        gprint22 = GPRINT(vdef22, 'MIN:%8.2lf%s')
        gprint32 = GPRINT(vdef32, 'MAX:%8.2lf%s\l')

        line3 = LINE(defObj=vdef33, color='#FFFFFFFF', legend='Active')
        gprint3 = GPRINT(vdef33, 'MAX:%8.2lf%s\l')

        area4 = AREA(defObj=cdef4, color='#000099', legend='Inactive_anon',
                     stack=True)
        gprint4 = GPRINT(vdef4, 'LAST:%8.2lf%s')
        gprint14 = GPRINT(vdef14, 'AVG:%8.2lf%s')
        gprint24 = GPRINT(vdef24, 'MIN:%8.2lf%s')
        gprint34 = GPRINT(vdef34, 'MAX:%8.2lf%s\l')

        area5 = AREA(defObj=cdef5, color='#0066ff', legend='Inactive_file',
                     stack=True)
        gprint5 = GPRINT(vdef5, 'LAST:%8.2lf%s')
        gprint15 = GPRINT(vdef15, 'AVG:%8.2lf%s')
        gprint25 = GPRINT(vdef25, 'MIN:%8.2lf%s')
        gprint35 = GPRINT(vdef35, 'MAX:%8.2lf%s\l')

        gprint6 = GPRINT(vdef36, 'MAX:%8.2lf%s\l')

        paramlist = [def1, cdef1, vdef1, vdef11, vdef21, vdef31,
                     def2, cdef2, vdef2, vdef12, vdef22, vdef32,
                     def3, cdef3, vdef33,
                     def4, cdef4, vdef4, vdef14, vdef24, vdef34,
                     def5, cdef5, vdef5, vdef15, vdef25, vdef35,
                     def6, cdef6, vdef36,
                     area1, gprint1, gprint11, gprint21, gprint31,
                     area2, gprint2, gprint12, gprint22, gprint32,
                     line3, gprint3,
                     area4, gprint4, gprint14, gprint24, gprint34,
                     area5, gprint5, gprint15, gprint25, gprint35,
                     gprint6]
        g = Graph(namespace.get('graphpath')+'memory_active.png',
                  start=(int(time.time())-gtime),
                  end=int(time.time()),
                  vertical_label='Memory_usage', color=ca)
        g.data.extend(paramlist)
        g.title = "Memory_active"
        g.width = namespace.get('gwidth')
        g.height = namespace.get('gheight')
        g.write()
        # #########################
        # Memory swap
        # #########################
        def1 = DEF(rrdfile=namespace.get('rrdpath'), vname='SwapFree',
                   dsName='SwapFree')
        def2 = DEF(rrdfile=namespace.get('rrdpath'), vname='SwapTotal',
                   dsName='SwapTotal')
        cdef1 = CDEF(vname='swap_free_c', rpn='%s,1024,*' % def1.vname)
        cdef2 = CDEF(vname='swap_total_c', rpn='%s,1024,*' % def2.vname)

        vdef1 = VDEF(vname='swap_free_last', rpn='%s,LAST' % cdef1.vname)
        vdef11 = VDEF(vname='swap_free_avg', rpn='%s,AVERAGE' % cdef1.vname)
        vdef21 = VDEF(vname='swap_free_min', rpn='%s,MINIMUM' % cdef1.vname)
        vdef31 = VDEF(vname='swap_free_max', rpn='%s,MAXIMUM' % cdef1.vname)
        vdef32 = VDEF(vname='swap_total_max', rpn='%s,MAXIMUM' % cdef2.vname)

        area1 = AREA(defObj=cdef1, color='#006600', legend='Swap_Free')
        gprint1 = GPRINT(vdef1, 'LAST:%8.2lf%s')
        gprint11 = GPRINT(vdef11, 'AVG:%8.2lf%s')
        gprint21 = GPRINT(vdef21, 'MIN:%8.2lf%s')
        gprint31 = GPRINT(vdef31, 'MAX:%8.2lf%s\l')

        line2 = LINE(defObj=vdef32, color='#FFFFFFFF', legend='Memory_Total')
        gprint2 = GPRINT(vdef31, 'MAX:%8.2lf%s\l')
        paramlist = [def1, cdef1, vdef1, vdef11, vdef21, vdef31,
                     def2, cdef2, vdef32,
                     area1, gprint1, gprint11, gprint21, gprint31,
                     line2, gprint31]

        g = Graph(namespace.get('graphpath')+'memory_swap.png',
                  start=(int(time.time())-gtime),
                  end=int(time.time()),
                  vertical_label='Memory_usage', color=ca)
        g.data.extend(paramlist)
        g.title = "Memory_swap"
        g.width = namespace.get('gwidth')
        g.height = namespace.get('gheight')
        g.write()
        # #########################
        # Memory pages
        # #########################
        def1 = DEF(rrdfile=namespace.get('rrdpath'), vname='Dirty',
                   dsName='Dirty')
        def2 = DEF(rrdfile=namespace.get('rrdpath'), vname='AnonPages',
                   dsName='AnonPages')
        def3 = DEF(rrdfile=namespace.get('rrdpath'), vname='HugePages_Free',
                   dsName='HugePages_Free')
        def4 = DEF(rrdfile=namespace.get('rrdpath'), vname='HugePages_Total',
                   dsName='HugePages_Total')

        cdef1 = CDEF(vname='dirty_c', rpn='%s,1024,*' % def1.vname)
        cdef2 = CDEF(vname='anonpages_c', rpn='%s,1024,*' % def2.vname)
        cdef3 = CDEF(vname='hugepages_free_c', rpn='%s,1024,*' % def3.vname)
        cdef4 = CDEF(vname='hugepages_total_c', rpn='%s,1024,*' % def4.vname)

        vdef1 = VDEF(vname='dirty_last', rpn='%s,LAST' % cdef1.vname)
        vdef2 = VDEF(vname='anonpages_last', rpn='%s,LAST' % cdef2.vname)
        vdef3 = VDEF(vname='hugepages_free_last', rpn='%s,LAST' % cdef3.vname)

        vdef11 = VDEF(vname='dirty_avg', rpn='%s,AVERAGE' % cdef1.vname)
        vdef12 = VDEF(vname='anonpages_avg', rpn='%s,AVERAGE' % cdef2.vname)
        vdef13 = VDEF(vname='hugepages_free_avg', rpn='%s,AVERAGE' % cdef3.vname)

        vdef21 = VDEF(vname='dirty_min', rpn='%s,MINIMUM' % cdef1.vname)
        vdef22 = VDEF(vname='anonpages_min', rpn='%s,MINIMUM' % cdef2.vname)
        vdef23 = VDEF(vname='hugepages_free_min', rpn='%s,MINIMUM' % cdef3.vname)

        vdef31 = VDEF(vname='dirty_max', rpn='%s,MAXIMUM' % cdef1.vname)
        vdef32 = VDEF(vname='anonpages_max', rpn='%s,MAXIMUM' % cdef2.vname)
        vdef33 = VDEF(vname='hugepages_free_max', rpn='%s,MAXIMUM' % cdef3.vname)
        vdef34 = VDEF(vname='hugepages_total_max', rpn='%s,MAXIMUM' % cdef4.vname)

        area1 = AREA(defObj=cdef1, color='#FFF200FF', legend='Dirty',
                     stack=True)
        gprint1 = GPRINT(vdef1, 'LAST:%8.2lf%s')
        gprint11 = GPRINT(vdef11, 'AVG:%8.2lf%s')
        gprint21 = GPRINT(vdef21, 'MIN:%8.2lf%s')
        gprint31 = GPRINT(vdef31, 'MAX:%8.2lf%s\l')

        area2 = AREA(defObj=cdef2, color='#6EA100FF', legend='AnonPages',
                     stack=True)
        gprint2 = GPRINT(vdef2, 'LAST:%8.2lf%s')
        gprint12 = GPRINT(vdef12, 'AVG:%8.2lf%s')
        gprint22 = GPRINT(vdef22, 'MIN:%8.2lf%s')
        gprint32 = GPRINT(vdef32, 'MAX:%8.2lf%s\l')

        area3 = AREA(defObj=cdef3, color='#12B3B5FF', legend='HugePages_Free',
                     stack=True)
        gprint3 = GPRINT(vdef3, 'LAST:%8.2lf%s')
        gprint13 = GPRINT(vdef13, 'AVG:%8.2lf%s')
        gprint23 = GPRINT(vdef23, 'MIN:%8.2lf%s')
        gprint33 = GPRINT(vdef33, 'MAX:%8.2lf%s\l')

        line4 = LINE(defObj=vdef34, color='#FFFFFFFF', legend='HugePages_Total')
        gprint4 = GPRINT(vdef34, 'MAX:%8.2lf%s\l')

        paramlist = [def1, cdef1, vdef1, vdef11, vdef21, vdef31,
                     def2, cdef2, vdef2, vdef12, vdef22, vdef32,
                     def3, cdef3, vdef3, vdef13, vdef23, vdef33,
                     def4, cdef4, vdef34,
                     area1, gprint1, gprint11, gprint21, gprint31,
                     area2, gprint2, gprint12, gprint22, gprint32,
                     area3, gprint3, gprint13, gprint23, gprint33,
                     line4, gprint4]

        g = Graph(namespace.get('graphpath')+'memory_pages.png',
                  start=(int(time.time())-gtime),
                  end=int(time.time()),
                  vertical_label='Memory_usage', color=ca)
        g.data.extend(paramlist)
        g.title = "Memory_Pages"
        g.width = namespace.get('gwidth')
        g.height = namespace.get('gheight')
        g.write()

    if cpuvalues:
        ######################
        # CPU LOAD AVERAGE
        # ####################
        def1 = DEF(rrdfile=namespace.get('rrdpath'), vname='loadavg1min',
                   dsName='loadavg1min')
        def2 = DEF(rrdfile=namespace.get('rrdpath'), vname='loadavg5min',
                   dsName='loadavg5min')
        def3 = DEF(rrdfile=namespace.get('rrdpath'), vname='loadavg15min',
                   dsName='loadavg15min')

        cdef1 = CDEF(vname='loadavg1min_c', rpn='%s,1,*' % def1.vname)
        cdef2 = CDEF(vname='loadavg5min_c', rpn='%s,1,*' % def2.vname)
        cdef3 = CDEF(vname='loadavg15min_c', rpn='%s,1,*' % def3.vname)

        vdef1 = VDEF(vname='loadavg1min_last', rpn='%s,LAST' % cdef1.vname)
        vdef2 = VDEF(vname='loadavg5min_last', rpn='%s,LAST' % cdef2.vname)
        vdef3 = VDEF(vname='loadavg15min_last', rpn='%s,LAST' % cdef3.vname)

        vdef11 = VDEF(vname='loadavg1min_avg', rpn='%s,AVERAGE' % cdef1.vname)
        vdef12 = VDEF(vname='loadavg5min_avg', rpn='%s,AVERAGE' % cdef2.vname)
        vdef13 = VDEF(vname='loadavg15min_avg', rpn='%s,AVERAGE' % cdef3.vname)

        vdef21 = VDEF(vname='loadavg1min_min', rpn='%s,MINIMUM' % cdef1.vname)
        vdef22 = VDEF(vname='loadavg5min_min', rpn='%s,MINIMUM' % cdef2.vname)
        vdef23 = VDEF(vname='loadavg15min_min', rpn='%s,MINIMUM' % cdef3.vname)

        vdef31 = VDEF(vname='loadavg1min_max', rpn='%s,MAXIMUM' % cdef1.vname)
        vdef32 = VDEF(vname='loadavg5min_max', rpn='%s,MAXIMUM' % cdef2.vname)
        vdef33 = VDEF(vname='loadavg15min_max', rpn='%s,MAXIMUM' % cdef3.vname)

        constline1 = LINE(value=100, color='#990000', legend='Max 100%')
        line1 = LINE(defObj=cdef1, color='#FFFFFFFF', legend='Load AVG 1 Min')
        gprint1 = GPRINT(vdef1, 'LAST:%3.2lf')
        gprint11 = GPRINT(vdef11, 'AVG:%3.2lf')
        gprint21 = GPRINT(vdef21, 'MIN:%3.2lf')
        gprint31 = GPRINT(vdef31, 'MAX:%3.2lf\l')

        line2 = LINE(defObj=cdef2, color='#6EA100FF', legend='Load AVG 5 Min')
        gprint2 = GPRINT(vdef2, 'LAST:%3.2lf')
        gprint12 = GPRINT(vdef12, 'AVG:%3.2lf')
        gprint22 = GPRINT(vdef22, 'MIN:%3.2lf')
        gprint32 = GPRINT(vdef32, 'MAX:%3.2lf\l')

        area3 = AREA(defObj=cdef3, color='#12B3B5FF', legend='Load AVG 15 Min',
                     stack=True)
        gprint3 = GPRINT(vdef3, 'LAST:%3.2lf')
        gprint13 = GPRINT(vdef13, 'AVG:%3.2lf')
        gprint23 = GPRINT(vdef23, 'MIN:%3.2lf')
        gprint33 = GPRINT(vdef33, 'MAX:%3.2lf\l')

        paramlist = [def1, cdef1, vdef1, vdef11, vdef21, vdef31,
                     def2, cdef2, vdef2, vdef12, vdef22, vdef32,
                     def3, cdef3, vdef3, vdef13, vdef23, vdef33,
                     area3, gprint3, gprint13, gprint23, gprint33,
                     line2, gprint2, gprint12, gprint22, gprint32,
                     line1, gprint1, gprint11, gprint21, gprint31,
                     constline1]

        g = Graph(namespace.get('graphpath')+'cpu_loadavg.png',
                  start=(int(time.time())-gtime),
                  end=int(time.time()),
                  vertical_label='CPU_utilization', color=ca)
        g.data.extend(paramlist)
        g.title = "Load_Average"
        g.width = namespace.get('gwidth')
        g.height = namespace.get('gheight')
        g.write()
        for cpu in cpuvalues.itervalues():
            ################################
            # CPU Utilization for each cpu
            # #############################
            cn = cpu.get('name')
            def1 = DEF(rrdfile=namespace.get('rrdpath'), vname='cpu_system',
                       dsName='%s_system' % cn)
            def2 = DEF(rrdfile=namespace.get('rrdpath'), vname='cpu_user',
                       dsName='%s_user' % cn)
            def3 = DEF(rrdfile=namespace.get('rrdpath'), vname='cpu_idle',
                       dsName='%s_idle' % cn)
            def4 = DEF(rrdfile=namespace.get('rrdpath'), vname='cpu_iowait',
                       dsName='%s_iowait' % cn)
            def5 = DEF(rrdfile=namespace.get('rrdpath'), vname='cpu_irq',
                       dsName='%s_irq' % cn)
            def6 = DEF(rrdfile=namespace.get('rrdpath'), vname='cpu_softirq',
                       dsName='%s_softirq' % cn)
            def7 = DEF(rrdfile=namespace.get('rrdpath'), vname='cpu_nice',
                       dsName='%s_nice' % cn)
            def8 = DEF(rrdfile=namespace.get('rrdpath'), vname='cpu_steal',
                       dsName='%s_steal' % cn)
            def9 = DEF(rrdfile=namespace.get('rrdpath'), vname='cpu_guest',
                       dsName='%s_guest' % cn)

            vdef1 = VDEF(vname='cpu_system_last', rpn='%s,LAST' % def1.vname)
            vdef2 = VDEF(vname='cpu_user_last', rpn='%s,LAST' % def2.vname)
            vdef3 = VDEF(vname='cpu_idle_last', rpn='%s,LAST' % def3.vname)
            vdef4 = VDEF(vname='cpu_iowait_last', rpn='%s,LAST' % def4.vname)
            vdef5 = VDEF(vname='cpu_irq_last', rpn='%s,LAST' % def5.vname)
            vdef6 = VDEF(vname='cpu_softirq_last', rpn='%s,LAST' % def6.vname)
            vdef7 = VDEF(vname='cpu_nice_last', rpn='%s,LAST' % def7.vname)
            vdef8 = VDEF(vname='cpu_steal_last', rpn='%s,LAST' % def8.vname)
            vdef9 = VDEF(vname='cpu_guest_last', rpn='%s,LAST' % def9.vname)

            vdef11 = VDEF(vname='cpu_system_avg', rpn='%s,AVERAGE' % def1.vname)
            vdef12 = VDEF(vname='cpu_user_avg', rpn='%s,AVERAGE' % def2.vname)
            vdef13 = VDEF(vname='cpu_idle_avg', rpn='%s,AVERAGE' % def3.vname)
            vdef14 = VDEF(vname='cpu_iowait_avg', rpn='%s,AVERAGE' % def4.vname)
            vdef15 = VDEF(vname='cpu_irq_avg', rpn='%s,AVERAGE' % def5.vname)
            vdef16 = VDEF(vname='cpu_softirq_avg', rpn='%s,AVERAGE' % def6.vname)
            vdef17 = VDEF(vname='cpu_nice_avg', rpn='%s,AVERAGE' % def7.vname)
            vdef18 = VDEF(vname='cpu_steal_avg', rpn='%s,AVERAGE' % def8.vname)
            vdef19 = VDEF(vname='cpu_guest_avg', rpn='%s,AVERAGE' % def9.vname)

            vdef21 = VDEF(vname='cpu_system_min', rpn='%s,MINIMUM' % def1.vname)
            vdef22 = VDEF(vname='cpu_user_min', rpn='%s,MINIMUM' % def2.vname)
            vdef23 = VDEF(vname='cpu_idle_min', rpn='%s,MINIMUM' % def3.vname)
            vdef24 = VDEF(vname='cpu_iowait_min', rpn='%s,MINIMUM' % def4.vname)
            vdef25 = VDEF(vname='cpu_irq_min', rpn='%s,MINIMUM' % def5.vname)
            vdef26 = VDEF(vname='cpu_softirq_min', rpn='%s,MINIMUM' % def6.vname)
            vdef27 = VDEF(vname='cpu_nice_min', rpn='%s,MINIMUM' % def7.vname)
            vdef28 = VDEF(vname='cpu_steal_min', rpn='%s,MINIMUM' % def8.vname)
            vdef29 = VDEF(vname='cpu_guest_min', rpn='%s,MINIMUM' % def9.vname)

            vdef31 = VDEF(vname='cpu_system_max', rpn='%s,MAXIMUM' % def1.vname)
            vdef32 = VDEF(vname='cpu_user_max', rpn='%s,MAXIMUM' % def2.vname)
            vdef33 = VDEF(vname='cpu_idle_max', rpn='%s,MAXIMUM' % def3.vname)
            vdef34 = VDEF(vname='cpu_iowait_max', rpn='%s,MAXIMUM' % def4.vname)
            vdef35 = VDEF(vname='cpu_irq_max', rpn='%s,MAXIMUM' % def5.vname)
            vdef36 = VDEF(vname='cpu_softirq_max', rpn='%s,MAXIMUM' % def6.vname)
            vdef37 = VDEF(vname='cpu_nice_max', rpn='%s,MAXIMUM' % def7.vname)
            vdef38 = VDEF(vname='cpu_steal_max', rpn='%s,MAXIMUM' % def8.vname)
            vdef39 = VDEF(vname='cpu_guest_max', rpn='%s,MAXIMUM' % def9.vname)

            area1 = AREA(defObj=def1, color='#ff0000', legend=def1.vname)
            gprint1 = GPRINT(vdef1, 'LAST:%3.2lf')
            gprint11 = GPRINT(vdef11, 'AVG:%3.2lf')
            gprint21 = GPRINT(vdef21, 'MIN:%3.2lf')
            gprint31 = GPRINT(vdef31, 'MAX:%3.2lf\l')

            area2 = AREA(defObj=def2, color='#ff8000', legend=def2.vname,
                         stack=True)
            gprint2 = GPRINT(vdef2, 'LAST:%3.2lf')
            gprint12 = GPRINT(vdef12, 'AVG:%3.2lf')
            gprint22 = GPRINT(vdef22, 'MIN:%3.2lf')
            gprint32 = GPRINT(vdef32, 'MAX:%3.2lf\l')

            area3 = AREA(defObj=def3, color='#ffff00', legend=def3.vname,
                         stack=True)
            gprint3 = GPRINT(vdef3, 'LAST:%3.2lf')
            gprint13 = GPRINT(vdef13, 'AVG:%3.2lf')
            gprint23 = GPRINT(vdef23, 'MIN:%3.2lf')
            gprint33 = GPRINT(vdef33, 'MAX:%3.2lf\l')

            area4 = AREA(defObj=def4, color='#80ff00', legend=def4.vname,
                         stack=True)
            gprint4 = GPRINT(vdef4, 'LAST:%3.2lf')
            gprint14 = GPRINT(vdef14, 'AVG:%3.2lf')
            gprint24 = GPRINT(vdef24, 'MIN:%3.2lf')
            gprint34 = GPRINT(vdef34, 'MAX:%3.2lf\l')

            area5 = AREA(defObj=def5, color='#00ff00', legend=def5.vname,
                         stack=True)
            gprint5 = GPRINT(vdef5, 'LAST:%3.2lf')
            gprint15 = GPRINT(vdef15, 'AVG:%3.2lf')
            gprint25 = GPRINT(vdef25, 'MIN:%3.2lf')
            gprint35 = GPRINT(vdef35, 'MAX:%3.2lf\l')

            area6 = AREA(defObj=def6, color='#00ff80', legend=def6.vname,
                         stack=True)
            gprint6 = GPRINT(vdef6, 'LAST:%3.2lf')
            gprint16 = GPRINT(vdef16, 'AVG:%3.2lf')
            gprint26 = GPRINT(vdef26, 'MIN:%3.2lf')
            gprint36 = GPRINT(vdef36, 'MAX:%3.2lf\l')

            area7 = AREA(defObj=def7, color='#00ffff', legend=def7.vname,
                         stack=True)
            gprint7 = GPRINT(vdef7, 'LAST:%3.2lf')
            gprint17 = GPRINT(vdef17, 'AVG:%3.2lf')
            gprint27 = GPRINT(vdef27, 'MIN:%3.2lf')
            gprint37 = GPRINT(vdef37, 'MAX:%3.2lf\l')

            area8 = AREA(defObj=def8, color='#00bfff', legend=def8.vname,
                         stack=True)
            gprint8 = GPRINT(vdef8, 'LAST:%3.2lf')
            gprint18 = GPRINT(vdef18, 'AVG:%3.2lf')
            gprint28 = GPRINT(vdef28, 'MIN:%3.2lf')
            gprint38 = GPRINT(vdef38, 'MAX:%3.2lf\l')

            area9 = AREA(defObj=def9, color='#0040ff', legend=def9.vname,
                         stack=True)
            gprint9 = GPRINT(vdef9, 'LAST:%3.2lf')
            gprint19 = GPRINT(vdef19, 'AVG:%3.2lf')
            gprint29 = GPRINT(vdef29, 'MIN:%3.2lf')
            gprint39 = GPRINT(vdef39, 'MAX:%3.2lf\l')

            paramlist = [def1, vdef1, vdef11, vdef21, vdef31,
                         def2, vdef2, vdef12, vdef22, vdef32,
                         def3, vdef3, vdef13, vdef23, vdef33,
                         def4, vdef4, vdef14, vdef24, vdef34,
                         def5, vdef5, vdef15, vdef25, vdef35,
                         def6, vdef6, vdef16, vdef26, vdef36,
                         def7, vdef7, vdef17, vdef27, vdef37,
                         def8, vdef8, vdef18, vdef28, vdef38,
                         def9, vdef9, vdef19, vdef29, vdef39,
                         area1, gprint1, gprint11, gprint21, gprint31,
                         area2, gprint2, gprint12, gprint22, gprint32,
                         area4, gprint4, gprint14, gprint24, gprint34,
                         area5, gprint5, gprint15, gprint25, gprint33,
                         area6, gprint6, gprint16, gprint26, gprint33,
                         area7, gprint7, gprint17, gprint27, gprint37,
                         area8, gprint8, gprint18, gprint28, gprint38,
                         area9, gprint9, gprint19, gprint29, gprint39,
                         area3, gprint3, gprint13, gprint23, gprint33,
                         ]
            g = Graph(namespace.get('graphpath')+'%s_util.png' % cn,
                      start=(int(time.time())-gtime),
                      end=int(time.time()),
                      vertical_label='Load', color=ca)
            g.data.extend(paramlist)
            g.title = "%s_utilizaton_for_%s_seconds" % (cn, gtime)
            g.width = namespace.get('gwidth')
            g.height = namespace.get('gheight')
            g.write()
    if netvalues:
        for interface in netvalues.items():
            ##################################
            # INTERFACE BYTES
            # ################################
            ifname = interface[0].strip(' ')
            def1 = DEF(rrdfile=namespace.get('rrdpath'), vname='if_recv_bytes',
                       dsName='%s_recv_bytes' % ifname)
            def2 = DEF(rrdfile=namespace.get('rrdpath'), vname='if_trans_bytes',
                       dsName='%s_trans_bytes' % ifname)
            cdef1 = CDEF(vname='if_recv_bytes_c', rpn='%s,1000000,/' % def1.vname)
            cdef2 = CDEF(vname='if_trans_bytes_c', rpn='%s,1000000,/' % def2.vname)

            vdef31 = VDEF(vname='if_recv_bytes_max', rpn='%s,MAXIMUM' % cdef1.vname)
            vdef32 = VDEF(vname='if_trans_bytes_max', rpn='%s,MAXIMUM' % cdef2.vname)

            area1 = AREA(defObj=def1, color='#339933', legend=def1.vname)
            gprint31 = GPRINT(vdef31, 'Total:%3.2lf MBytes\l')

            line2 = LINE(defObj=def2, color='#0000ff', legend=def2.vname)
            gprint32 = GPRINT(vdef32, 'Total:%3.2lf MBytes\l')

            paramlist = [def1, cdef1, vdef31,
                         def2, cdef2, vdef32,
                         area1, gprint31,
                         line2, gprint32,
                         ]
            g = Graph(namespace.get('graphpath')+'%s_bytes.png' % ifname,
                      start=(int(time.time())-gtime),
                      end=int(time.time()),
                      vertical_label='Traffic,Bytes', color=ca)
            g.data.extend(paramlist)
            g.title = "%s_utilizaton_for_%s_seconds" % (ifname, gtime)
            g.width = namespace.get('gwidth')
            g.height = namespace.get('gheight')
            g.write()
            ######################################
            # INTERFACE PACKETS
            #####################################
            def3 = DEF(rrdfile=namespace.get('rrdpath'), vname='if_recv_packets',
                       dsName='%s_recv_packets' % ifname)
            def4 = DEF(rrdfile=namespace.get('rrdpath'), vname='if_trans_packets',
                       dsName='%s_trans_packets' % ifname)
            def5 = DEF(rrdfile=namespace.get('rrdpath'), vname='if_recv_errs',
                       dsName='%s_recv_errs' % ifname)
            def6 = DEF(rrdfile=namespace.get('rrdpath'), vname='if_trans_errs',
                       dsName='%s_trans_errs' % ifname)
            vdef3 = VDEF(vname='if_recv_packets_last', rpn='%s,LAST' % def3.vname)
            vdef4 = VDEF(vname='if_trans_packets_last', rpn='%s,LAST' % def4.vname)
            vdef5 = VDEF(vname='if_recv_errs_last', rpn='%s,LAST' % def5.vname)
            vdef6 = VDEF(vname='if_trans_errs_last', rpn='%s,LAST' % def6.vname)
            vdef13 = VDEF(vname='if_recv_packets_avg', rpn='%s,AVERAGE' % def3.vname)
            vdef14 = VDEF(vname='if_trans_packets_avg', rpn='%s,AVERAGE' % def4.vname)
            vdef15 = VDEF(vname='if_recv_errs_avg', rpn='%s,AVERAGE' % def5.vname)
            vdef16 = VDEF(vname='if_trans_errs_avg', rpn='%s,AVERAGE' % def6.vname)

            vdef23 = VDEF(vname='if_recv_packets_min', rpn='%s,MINIMUM' % def3.vname)
            vdef24 = VDEF(vname='if_trans_packets_min', rpn='%s,MINIMUM' % def4.vname)
            vdef25 = VDEF(vname='if_recv_errs_min', rpn='%s,MINIMUM' % def5.vname)
            vdef26 = VDEF(vname='if_trans_errs_min', rpn='%s,MINIMUM' % def6.vname)

            vdef33 = VDEF(vname='if_recv_packets_max', rpn='%s,MAXIMUM' % def3.vname)
            vdef34 = VDEF(vname='if_trans_packets_max', rpn='%s,MAXIMUM' % def4.vname)
            vdef35 = VDEF(vname='if_recv_errs_max', rpn='%s,MAXIMUM' % def5.vname)
            vdef36 = VDEF(vname='if_trans_errs_max', rpn='%s,MAXIMUM' % def6.vname)

            area3 = AREA(defObj=def3, color='#006600', legend=def3.vname)
            gprint3 = GPRINT(vdef3, 'LAST:%3.2lf')
            gprint13 = GPRINT(vdef13, 'AVG:%3.2lf')
            gprint23 = GPRINT(vdef23, 'MIN:%3.2lf')
            gprint33 = GPRINT(vdef33, 'MAX:%3.2lf\l')

            line4 = LINE(defObj=def4, color='#0000ff', legend=def4.vname)
            gprint4 = GPRINT(vdef4, 'LAST:%3.2lf')
            gprint14 = GPRINT(vdef14, 'AVG:%3.2lf')
            gprint24 = GPRINT(vdef24, 'MIN:%3.2lf')
            gprint34 = GPRINT(vdef34, 'MAX:%3.2lf\l')

            line5 = LINE(defObj=def5, color='#ffff00', legend=def5.vname)
            gprint5 = GPRINT(vdef5, 'LAST:%3.2lf')
            gprint15 = GPRINT(vdef15, 'AVG:%3.2lf')
            gprint25 = GPRINT(vdef25, 'MIN:%3.2lf')
            gprint35 = GPRINT(vdef35, 'MAX:%3.2lf\l')

            line6 = LINE(defObj=def6, color='#ff0000', legend=def6.vname)
            gprint6 = GPRINT(vdef6, 'LAST:%3.2lf')
            gprint16 = GPRINT(vdef16, 'AVG:%3.2lf')
            gprint26 = GPRINT(vdef26, 'MIN:%3.2lf')
            gprint36 = GPRINT(vdef36, 'MAX:%3.2lf\l')

            paramlist = [def3, vdef3, vdef13, vdef23, vdef33,
                         def4, vdef4, vdef14, vdef24, vdef34,
                         def5, vdef5, vdef15, vdef25, vdef35,
                         def6, vdef6, vdef16, vdef26, vdef36,
                         area3, gprint3, gprint13, gprint23, gprint33,
                         line4, gprint4, gprint14, gprint24, gprint34,
                         line5, gprint5, gprint15, gprint25, gprint33,
                         line6, gprint6, gprint16, gprint26, gprint33,
                         ]
            g = Graph(namespace.get('graphpath')+'%s_packets.png' % ifname,
                      start=(int(time.time())-gtime),
                      end=int(time.time()),
                      vertical_label='Packets_per_second', color=ca)
            g.data.extend(paramlist)
            g.title = "%s_packets_for_%s_seconds" % (ifname, gtime)
            g.width = namespace.get('gwidth')
            g.height = namespace.get('gheight')
            g.write()

    if blockvalues:
        for blockdevice in blockvalues.items():
            ######################
            # BLOCK DEVICE MS
            # ####################
            devname = blockdevice[0].strip(' ')
            def1 = DEF(rrdfile=namespace.get('rrdpath'), vname='dev_ms_doing_io',
                       dsName='%s_ms_doing_io' % devname)
            def2 = DEF(rrdfile=namespace.get('rrdpath'), vname='dev_ms_writing',
                       dsName='%s_ms_writing' % devname)
            def3 = DEF(rrdfile=namespace.get('rrdpath'), vname='dev_ms_weighted',
                       dsName='%s_ms_weighted' % devname)
            def4 = DEF(rrdfile=namespace.get('rrdpath'), vname='dev_ms_reading',
                       dsName='%s_ms_reading' % devname)

            cdef1 = CDEF(vname='dev_ms_doing_io_c', rpn='%s,1,/' % def1.vname)
            cdef2 = CDEF(vname='dev_ms_writing_c', rpn='%s,1,/' % def2.vname)
            cdef3 = CDEF(vname='dev_ms_weighted_c', rpn='%s,1,/' % def3.vname)
            cdef4 = CDEF(vname='dev_ms_reading_c', rpn='%s,1,/' % def4.vname)

            vdef1 = VDEF(vname='dev_ms_doing_io_last', rpn='%s,LAST' % cdef1.vname)
            vdef2 = VDEF(vname='dev_ms_writing_last', rpn='%s,LAST' % cdef2.vname)
            vdef3 = VDEF(vname='dev_ms_weighted_last', rpn='%s,LAST' % cdef3.vname)
            vdef4 = VDEF(vname='dev_ms_reading_last', rpn='%s,LAST' % cdef4.vname)

            vdef11 = VDEF(vname='dev_ms_doing_io_avg', rpn='%s,AVERAGE' % cdef1.vname)
            vdef12 = VDEF(vname='dev_ms_writing_avg', rpn='%s,AVERAGE' % cdef2.vname)
            vdef13 = VDEF(vname='dev_ms_weighted_avg', rpn='%s,AVERAGE' % cdef3.vname)
            vdef14 = VDEF(vname='dev_ms_reading_avg', rpn='%s,AVERAGE' % cdef4.vname)

            vdef21 = VDEF(vname='dev_ms_doing_io_min', rpn='%s,MINIMUM' % cdef1.vname)
            vdef22 = VDEF(vname='dev_ms_writing_min', rpn='%s,MINIMUM' % cdef2.vname)
            vdef23 = VDEF(vname='dev_ms_weighted_min', rpn='%s,MINIMUM' % cdef3.vname)
            vdef24 = VDEF(vname='dev_ms_reading_min', rpn='%s,MINIMUM' % cdef4.vname)

            vdef31 = VDEF(vname='dev_ms_doing_io_max', rpn='%s,MAXIMUM' % cdef1.vname)
            vdef32 = VDEF(vname='dev_ms_writing_max', rpn='%s,MAXIMUM' % cdef2.vname)
            vdef33 = VDEF(vname='dev_ms_weighted_max', rpn='%s,MAXIMUM' % cdef3.vname)
            vdef34 = VDEF(vname='dev_ms_reading_max', rpn='%s,MAXIMUM' % cdef4.vname)

            line1 = LINE(defObj=cdef1, color='#006600', legend=def1.vname)
            gprint1 = GPRINT(vdef1, 'LAST:%3.2lf')
            gprint11 = GPRINT(vdef11, 'AVG:%3.2lf')
            gprint21 = GPRINT(vdef21, 'MIN:%3.2lf')
            gprint31 = GPRINT(vdef31, 'MAX:%3.2lf\l')

            line2 = LINE(defObj=cdef2, color='#0000ff', legend=def2.vname)
            gprint2 = GPRINT(vdef2, 'LAST:%3.2lf')
            gprint12 = GPRINT(vdef12, 'AVG:%3.2lf')
            gprint22 = GPRINT(vdef22, 'MIN:%3.2lf')
            gprint32 = GPRINT(vdef32, 'MAX:%3.2lf\l')

            line3 = LINE(defObj=cdef3, color='#ffff00', legend=def3.vname)
            gprint3 = GPRINT(vdef3, 'LAST:%3.2lf')
            gprint13 = GPRINT(vdef13, 'AVG:%3.2lf')
            gprint23 = GPRINT(vdef23, 'MIN:%3.2lf')
            gprint33 = GPRINT(vdef33, 'MAX:%3.2lf\l')

            line4 = LINE(defObj=cdef4, color='#ff0000', legend=def4.vname)
            gprint4 = GPRINT(vdef4, 'LAST:%3.2lf')
            gprint14 = GPRINT(vdef14, 'AVG:%3.2lf')
            gprint24 = GPRINT(vdef24, 'MIN:%3.2lf')
            gprint34 = GPRINT(vdef34, 'MAX:%3.2lf\l')

            paramlist = [def1, cdef1, vdef1, vdef11, vdef21, vdef31,
                         def2, cdef2, vdef2, vdef12, vdef22, vdef32,
                         def3, cdef3, vdef3, vdef13, vdef23, vdef33,
                         def4, cdef4, vdef4, vdef14, vdef24, vdef34,
                         line1, gprint1, gprint11, gprint21, gprint31,
                         line2, gprint2, gprint12, gprint22, gprint32,
                         line3, gprint3, gprint13, gprint23, gprint33,
                         line4, gprint4, gprint14, gprint24, gprint34
                         ]
            g = Graph(namespace.get('graphpath')+'%s_msstat.png' % devname,
                      start=(int(time.time())-gtime),
                      end=int(time.time()),
                      vertical_label='ms', color=ca)
            g.data.extend(paramlist)
            g.title = "%s_msstat_for_%s_seconds" % (devname, gtime)
            g.width = namespace.get('gwidth')
            g.height = namespace.get('gheight')
            g.write()
            ######################
            # BLOCK DEVICE IOS
            # ####################

            def1 = DEF(rrdfile=namespace.get('rrdpath'), vname='dev_writes',
                       dsName='%s_writes' % devname)
            def2 = DEF(rrdfile=namespace.get('rrdpath'), vname='dev_reads',
                       dsName='%s_reads' % devname)
            def3 = DEF(rrdfile=namespace.get('rrdpath'), vname='dev_cur_ios',
                       dsName='%s_cur_ios' % devname)

            cdef1 = CDEF(vname='dev_writes_c', rpn='%s,1,/' % def1.vname)
            cdef2 = CDEF(vname='dev_reads_c', rpn='%s,1,/' % def2.vname)
            cdef3 = CDEF(vname='dev_cur_ios_c', rpn='%s,1,/' % def3.vname)

            vdef1 = VDEF(vname='dev_writes_last', rpn='%s,LAST' % cdef1.vname)
            vdef2 = VDEF(vname='dev_reads_last', rpn='%s,LAST' % cdef2.vname)
            vdef3 = VDEF(vname='dev_cur_ios_last', rpn='%s,LAST' % cdef3.vname)

            vdef11 = VDEF(vname='dev_writes_avg', rpn='%s,AVERAGE' % cdef1.vname)
            vdef12 = VDEF(vname='dev_reads_avg', rpn='%s,AVERAGE' % cdef2.vname)
            vdef13 = VDEF(vname='dev_cur_ios_avg', rpn='%s,AVERAGE' % cdef3.vname)

            vdef21 = VDEF(vname='dev_writes_min', rpn='%s,MINIMUM' % cdef1.vname)
            vdef22 = VDEF(vname='dev_reads_min', rpn='%s,MINIMUM' % cdef2.vname)
            vdef23 = VDEF(vname='dev_cur_ios_min', rpn='%s,MINIMUM' % cdef3.vname)

            vdef31 = VDEF(vname='dev_writes_max', rpn='%s,MAXIMUM' % cdef1.vname)
            vdef32 = VDEF(vname='dev_reads_max', rpn='%s,MAXIMUM' % cdef2.vname)
            vdef33 = VDEF(vname='dev_cur_ios_max', rpn='%s,MAXIMUM' % cdef3.vname)

            area1 = AREA(defObj=cdef1, color='#006600', legend=def1.vname)
            gprint1 = GPRINT(vdef1, 'LAST:%3.2lf')
            gprint11 = GPRINT(vdef11, 'AVG:%3.2lf')
            gprint21 = GPRINT(vdef21, 'MIN:%3.2lf')
            gprint31 = GPRINT(vdef31, 'MAX:%3.2lf\l')

            line2 = LINE(defObj=cdef2, color='#0000ff', legend=def2.vname)
            gprint2 = GPRINT(vdef2, 'LAST:%3.2lf')
            gprint12 = GPRINT(vdef12, 'AVG:%3.2lf')
            gprint22 = GPRINT(vdef22, 'MIN:%3.2lf')
            gprint32 = GPRINT(vdef32, 'MAX:%3.2lf\l')

            line3 = LINE(defObj=cdef3, color='#ffff00', legend=def3.vname)
            gprint3 = GPRINT(vdef3, 'LAST:%3.2lf')
            gprint13 = GPRINT(vdef13, 'AVG:%3.2lf')
            gprint23 = GPRINT(vdef23, 'MIN:%3.2lf')
            gprint33 = GPRINT(vdef33, 'MAX:%3.2lf\l')

            paramlist = [def1, cdef1, vdef1, vdef11, vdef21, vdef31,
                         def2, cdef2, vdef2, vdef12, vdef22, vdef32,
                         def3, cdef3, vdef3, vdef13, vdef23, vdef33,
                         area1, gprint1, gprint11, gprint21, gprint31,
                         line2, gprint2, gprint12, gprint22, gprint32,
                         line3, gprint3, gprint13, gprint23, gprint33,
                         ]
            g = Graph(namespace.get('graphpath')+'%s_ios.png' % devname,
                      start=(int(time.time())-gtime),
                      end=int(time.time()),
                      vertical_label='ios', color=ca)
            g.data.extend(paramlist)
            g.title = "%s_ios_for_%s_seconds" % (devname, gtime)
            g.width = namespace.get('gwidth')
            g.height = namespace.get('gheight')
            g.write()


# Main func
def main(namespace):
    memvalues = {}
    netvalues = {}
    blockvalues = {}
    cpuvalues = {}
    loadavgvalues = {}
    if namespace['memory']:
        memvalues = readMemValues()
        if namespace['verbose']:
            printmemvalues(memvalues)
    if namespace['cpu']:
        loadavgvalues = readLoadAvgValues()
        cpuvalues = readCpuValues()
        if namespace['verbose']:
            printcpuvalues(cpuvalues, loadavgvalues)
    if namespace['net']:
        netvalues = readNetValues(namespace.get('interface'))
        if namespace['verbose']:
            printnetvalues(netvalues)
    if namespace['block']:
        blockvalues = readBlockValues(namespace.get('disk'))
        if namespace['verbose']:
            printblockvalues(blockvalues)
    if os.path.isfile(namespace.get('rrdpath')):
        updaterra(namespace, memvalues, netvalues, blockvalues,
                  cpuvalues, loadavgvalues)
    else:
        print "File %s not found, creating new one" % namespace.get('rrdpath')
        createrra(namespace, memvalues, netvalues, blockvalues,
                  cpuvalues, loadavgvalues)
    if namespace['graph']:
        draw_file(namespace, memvalues, netvalues, blockvalues,
                  cpuvalues, loadavgvalues)

if __name__ == "__main__":
    parser = createParser()
    namespace_args = parser.parse_args()
    namespace = initnamespace(namespace_args)
    sys.exit(main(namespace))
