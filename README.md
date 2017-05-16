# collect_sysstat
Lightweight python script to gather some sysstats, store them in rrd and draw some grapths


## Getting Started
I've tested this script on Redhat/CentOS/OEL 6/7

### Examples
Pictures can be found in images directory 


### Prerequisites

This script require module PyRRD
```
https://pypi.python.org/pypi/PyRRD/
```

### Installing
Just copy collect_sysstat.py to a location which you like and add it to crontab, i.e.:
```
*/1 * * * * python /root/bin/collect_sysstat.py -g 2>/dev/null 1>/dev/null
```

## Configuration
Modify parameters below in collect_sysstat.py:
```
rrdpath = '/tmp/test.rrd'          # Path to rrd database file
interface_list = 'eth0'            # List of interfaces to obtain data
block_dev_list = 'vda vdb vdb1'    # List of block dev to obtain data
graphpath = '/var/www/'            # Path to store graph files
gwidth = 800                       # Width of output graphs
gheight = 600                      # Height of output graphs
gtime = 86400                      # Create graphs from gtime to NOW
```
