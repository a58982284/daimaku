#!/usr/bin/env python
#coding:utf-8
from ericsson_cloud.config.configresourcemanager import ConfigResourceManager
#from ericsson_cloud.hardware.runipmicommand import RunGenericCommand
import commands
import Queue
import threading
import time
import re
import pdb
import sys,os
from datetime import datetime
import argparse

dbg=False
sim=False           #?

global queueLock    #?

def debuginfo(msg):     #定义一个debuginfo函数
    if dbg:             #if Falese 下面的语句不执行
        sys.stderr.write(str(datetime.now())+" : %s \n" % msg)

def info(msg):
    sys.stderr.write(str(datetime.now())+" : %s \n" % msg)

def retrySimpleCmd(func):
    def retried_func(*args, **kwargs):
        # start from 0
        MAX_TRIES = 2
        RETRY_INTERVAL = 2
        tries = 0

        while True:
            debuginfo("try execute command at "+str(tries+1)+" time")
            return_code, output = func(*args, **kwargs)
            if return_code > 0 and tries < MAX_TRIES:
                debuginfo("simple command return code abnormal, with code %d and error msg is %s "
                          % (return_code,output))
                tries += 1
                time.sleep(RETRY_INTERVAL)
                continue
            break

            if return_code > 0 and tries >= MAX_TRIES:
                debuginfo("return code is %s \n" % str(return_code))
                raise retryException("Max retries reached, still no success!")
        return return_code, output
    return retried_func

@retrySimpleCmd
def SimpleCmd(CmdStr):
    status=0
    output=None

    if not sim:
        status, output = commands.getstatusoutput(CmdStr)
    else:
        output = (("In command simulation mode, just showing full command is %s \n") % CmdStr)
        debuginfo(output)
        status=0
    return status, output

class nodestatus(object):
    def __init__(self):
        self.ipconnectivity="NA"
        self.ipmiaccountstatus="NA"
        self.blade=None
        self.shelf=None
        self.blade_id=0
        self.shelf_id=0
        self.ip=""
        self.user=""
        self.password=""
        self.taskdone=False
        self.response=""
        self.businfo={}
        self.hwi="Not Found"
        self.sn=""
        self.uuid=""
        self.control=[]
        self.data=[]
        self.storage=[]
        self.tgtnics=[]
        self.opstatus="NA"
        self.optype="NA"

def fetchbusinfo(blade, nicnpname):
    return blade.nic_assignment[nicnpname]


def createNodesArray(config="/mnt/cee_config/config.yaml", tgtnics=None):
    res_cfg = (ConfigResourceManager.get_instance(
        config_yaml_path=config).resource_cfg)
    nodes=[]
    for shelf in res_cfg.shelves:
        for blade in shelf.blades:
            node=nodestatus()
            node.blade=blade
            node.shelf=shelf
            node.blade_id=blade.position
            node.shelf_id=shelf.position
            node.ip=blade.mgmt_ip
            node.user=blade.mgmt_user
            node.password=blade.mgmt_passwd
            if tgtnics is not None:
                for nicnpname in tgtnics:
                    tgtnicbusinfo=fetchbusinfo(blade, nicnpname)
                    node.tgtnics.append(tgtnicbusinfo)
            nodes.append(node)
            node.control.append(fetchbusinfo(blade, "control0"))
            node.control.append(fetchbusinfo(blade,"control1"))
            node.data.append(fetchbusinfo(blade, "data0"))
            node.data.append(fetchbusinfo(blade,"data1"))
            node.storage.append(fetchbusinfo(blade,"storage0"))
            node.storage.append(fetchbusinfo(blade,"storage1"))
    return nodes

class InfoNotFoundException(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return repr(self.msg)

class TransferException(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return repr(self.msg)

class DependencyNotFoundException(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return repr(self.msg)


class envchecker(object):

    def __init__(self, config='/mnt/cee_config/config.yaml',tgtnics=None):
        self.configfile=config
        self.nodes=createNodesArray(config,tgtnics)
        self.toollist=["./runipmicommand.py","/usr/bin/hwres"]
        self.tool="/usr/bin/hwres"
        #self.tool="/usr/bin/hwres"

    def checkexistence(self):
        for toolpath in self.toollist:
            if os.path.isfile(toolpath):
                self.tool=toolpath
                return True
        return False

    # requires objects, not the id string.
    def checkbmcipconnectivity(self,blade):
        ipconnectivitytpl="fping -c 1 -t 50 {ip}"
        ippingcmd=ipconnectivitytpl.format(ip=blade.ip)
        status, response=SimpleCmd(ippingcmd)
        return status, response

    def checkbmcipmiaccount(self,blade):
        command="fru print 0"
        ipmicmdtpl = "ipmitool -H {host} -I lanplus -U {user} -P {password} {command}"
        ipmicmd=ipmicmdtpl.format(host=blade.ip,user=blade.user,password=blade.password,
                                  command=command)
        status, response=SimpleCmd(ipmicmd)
        return status, response

    def _createscript(self, filename):
        scriptstr=r"""#!/bin/bash

iflist=$(ip a | grep -E "[0-9]+: eth[0-9]+:.*" | awk -F":| " '{print $3}')
[[ -z $iflist ]] && iflist=$(ip a | grep -E "[0-9]+: p[0-9]p[0-9]:.*" | awk -F":| " '{print $3}')
[[ -z $iflist ]] && iflist=$(ip a | grep -E "[0-9]+: [a-z0-9]+:.*" | awk -F":| " '{print $3}' | grep -v lo)

for ifname in $iflist; do
        businfo=$(ethtool -i $ifname | awk '/bus-info/ { print $2 }')
        macaddr=$(ip link show $ifname | awk '/link/ { print $2}')
        echo "$ifname,$macaddr $businfo"
done"""
        with open(filename, "w") as text_file:
            text_file.write(scriptstr)

    def gettoolname(self):
        if re.search("runipmicommand", self.tool):
            if os.access(self.tool, os.X_OK):
                return self.tool
            else:
                return "python "+self.tool
        return self.tool

    def _gettargetnip(self,blade):
        if self.checkexistence():
            # get serial number from shelf id and blade id
            toolname=self.gettoolname()
            debuginfo("the toolname is %s" % toolname)
            cmdtpl="{tool} {config} {shelf} {blade} {cmd}"
            GETSN=cmdtpl.format(tool=toolname, config=self.configfile, shelf=blade.shelf_id, blade=blade.blade_id, cmd="getsn")
            debuginfo("the serial number access command is %s" % GETSN)
            status, response=SimpleCmd(GETSN)
            if status==0:
                SN=response
            else:
                raise InfoNotFoundException("Serial Number not found for shelf %d, blade %d" % (blade.shelf_id, blade.blade_id))

            # get node ip from serial number
            GETNIP=cmdtpl.format(tool=toolname, config=self.configfile, shelf=blade.shelf_id, blade=blade.blade_id, cmd=("getnip %s" % SN))
            debuginfo(" the nip command is %s" % GETNIP )
            status, response = SimpleCmd(GETNIP)
            if status==0:
                NIP=response
                debuginfo(" the node ip for this shelf %s, blade %s is %s " % (blade.shelf_id, blade.blade_id, NIP))
                return NIP
            else:
                raise InfoNotFoundException(
                    "Node IP not found for shelf %d, blade %d" % (blade.shelf_id, blade.blade_id))

    def accessbusinfo(self,blade):
        if self.checkexistence():
            NIP=self._gettargetnip(blade)
            # generate embeded script
            scriptfilename = "/tmp/fetchPCIAddr.sh"
            self._createscript(scriptfilename)

            # transfer script to target node
            transfercmdtpl="scp -q {src} {dest}"
            transfercmd=transfercmdtpl.format(src=scriptfilename,dest="root@"+NIP+":"+scriptfilename)
            status,response=SimpleCmd(transfercmd)
            if status!=0:
                raise TransferException("File transferring has problem")

            # execute the script to fetch specified info
            remoteexeccmdtpl="ssh -q {ip} {cmd}"
            remotecmd=remoteexeccmdtpl.format(ip=NIP,cmd=("bash %s" % scriptfilename))
            status, response=SimpleCmd(remotecmd)
        else:
            raise DependencyNotFoundException("system is not dpia patched, this tool require system to be dpia patched.")
        return status, response

    def serverinfo(self,blade):
        toolname=self.gettoolname()
        if self.checkexistence():
            cmdtpl="{tool} {config} {shelf} {blade} {cmd}"
            OPE=cmdtpl.format(tool=toolname, config=self.configfile, shelf=blade.shelf_id, blade=blade.blade_id, cmd='chkhwi')
            debuginfo("OPE command is %s" % OPE )
            status, response=SimpleCmd(OPE)
            if status == 0:
                return status, response
            else:
                raise InfoNotFoundException("Hardware info not found for shelf %d, blade %d, error message is %s" %
                                            (blade.shelf_id,blade.blade_id, response))
        else:
            raise DependencyNotFoundException(
                "system is not dpia patched, this tool require system to be dpia patched. " )


    def _getnicname(self, blade, busid):
        return blade.businfo[busid]

    def disablenic(self,blade):
        status, response=self.accessbusinfo(blade)
        if status == 0:
            collectbusinfo(blade, response)
        NIP=self._gettargetnip(blade)

        for nic in blade.tgtnics:
            nicname=self._getnicname(blade,nic)
            cmdtpl="ssh {ip} ip link dev {nic} down"
            remotecmd=cmdtpl.format(ip=NIP, nic=nicname)
            blade.optype="disablenic"
            status, response=SimpleCmd(remotecmd)
            if status !=0:
                blade.opstatus="failed"
                raise RemoteExecutionException("Disable nic %s on shelf %s blade %s failed!" %
                                               (nicname, blade.shelf_id, blade.blade_id))
            else:
                blade.opstatus="successful"
                info("nic %s (%s) on blade shelf %s blade %s now enabled" %
                     (nic, nicname, blade.shelf_id, blade.blade_id))
                return status, response

    def enablenic(self,blade):
        status, response = self.accessbusinfo(blade)
        if status == 0:
            collectbusinfo(blade, response)
        NIP = self._gettargetnip(blade)
        blade.optype="enablenic"
        for nic in blade.tgtnics:
            nicname = self._getnicname(blade, nic)
            cmdtpl = "ssh {ip} ip link dev {nic} up"
            remotecmd = cmdtpl.format(ip=NIP, nic=nicname)
            status, response = SimpleCmd(remotecmd)
            if status != 0:
                blade.opstatus="failed"
                raise RemoteExecutionException(
                    "Disable nic %s on shelf %s blade %s failed!" % (nicname, blade.shelf_id, blade.blade_id))
            else:
                blade.opstatus="successful"
                info("nic %s (%s) on blade shelf %s blade %s now enabled" %
                     (nic, nicname, blade.shelf_id, blade.blade_id))
                return status, response

    def flashnic(self,blade):
        status, response = self.accessbusinfo(blade)
        if status == 0:
            collectbusinfo(blade, response)
        NIP = self._gettargetnip(blade)

        interval=2
        duration=20
        blade.optype="flashnic"
        for counter in range(20):
            self.disablenic(blade)
            time.sleep(interval)
            self.enablenic(blade)
            time.sleep(interval)
        blade.opstatus="successful"
        return True, None

    def enableuid(self,blade, timer):
        cmdtpl = "ipmitool -H {ip} -U {user} -P {passwd} -I lanplus {cmd}"
        #chassis identify on
        oncmd='chassis identify '+str(timer)
        #offcmd="0"
        #default="15"
        uidon=cmdtpl.format(ip=blade.ip, user=blade.user, passwd=blade.password, cmd=oncmd)
        blade.optype="enableuid"
        status, response=SimpleCmd(uidon)
        if status==0:
            blade.opstatus="successful"
            info("Light UID LED with timer %d for node in shelf %s, blade %s successfully" % (timer, blade.shelf_id, blade.blade_id))
            return status, response
        else:
            blade.opstatus="failed"
            raise RemoteExecutionException(
                "Light UID LED for node in shelf %s, blade %s failed!" % (blade.shelf_id, blade.blade_id))

    def disableuid(self,blade):
        cmdtpl = "ipmitool -H {ip} -U {user} -P {passwd} -I lanplus {cmd}"
        #chassis identify on
        #oncmd='force'
        offcmd="chassis identify 0"
        #default="15"
        uidoff=cmdtpl.format(ip=blade.ip, user=blade.user, passwd=blade.password, cmd=offcmd)
        blade.optype="disableuid"
        status, response=SimpleCmd(uidoff)
        if status==0:
            blade.opstatus="successful"
            info("Turn off UID LED for node in shelf %s, blade %s successfully" % (blade.shelf_id, blade.blade_id))
            return status, response
        else:
            blade.opstatus="failed"
            raise RemoteExecutionException(
                "Turn off UID LED for node in shelf %s, blade %s failed!" % (blade.shelf_id, blade.blade_id))

    def flashone(self, blade):
        cmdtpl = "ipmitool -H {ip} -U {user} -P {passwd} -I lanplus {cmd}"
        #chassis identify on
        oncmd='chassis identify force'
        #offcmd="0"
        #default="15"
        uidon=cmdtpl.format(ip=blade.ip, user=blade.user, passwd=blade.password, cmd=oncmd)
        blade.optype="enableuid"
        status, response=SimpleCmd(uidon)
        if status==0:
            blade.opstatus="successful"
            info("Light UID LED with timer %d for node in shelf %s, blade %s successfully" % (1, blade.shelf_id, blade.blade_id))
            return status, response
        else:
            blade.opstatus="failed"
            raise RemoteExecutionException(
                "Light UID LED for node in shelf %s, blade %s failed!" % (blade.shelf_id, blade.blade_id))
        time.sleep(1)
        self.disableuid(blade)

    def flashuidntime(self, blade, n):
        for i in range(n):
            self.flashone(blade)

    def flashuid(self, blade, duration=20):
        oninterval=int(blade.blade_id)+8
        offinterval=2
        blade.optype="flashuid"
        flashtimer=int(blade.blade_id)%15
        for counter in range(duration):
            self.enableuid(blade,flashtimer)
            time.sleep(oninterval)
            self.disableuid(blade)
            time.sleep(offinterval)
        blade.opstatus="successful"
        return 1, None

    def poweron(self,blade):
        toolname = self.gettoolname()
        cmdtpl = "{tool} {config} {shelf} {blade} {cmd}"
        OPE = cmdtpl.format(tool=toolname, config=self.configfile, shelf=blade.shelf_id, blade=blade.blade_id,
                              cmd="setpon")
        blade.optype = "poweron"
        status, response = SimpleCmd(OPE)
        if status==0:
            blade.opstatus="successful"
            info("node shelf %s blade %s powered on succeed!" % (blade.shelf_id, blade.blade_id))
            return status, response
        else:
            blade.opstatus="failed"
            raise ExecutionException(
                "attemp to power on node shelf %s blade %s failed!" % (blade.shelf_id, blade.blade_id))

    def poweroff(self,blade):
        toolname = self.gettoolname()
        cmdtpl = "{tool} {config} {shelf} {blade} {cmd}"
        OPE = cmdtpl.format(tool=toolname, config=self.configfile, shelf=blade.shelf_id, blade=blade.blade_id,
                              cmd="setpof")
        blade.optype="poweroff"
        status, response = SimpleCmd(OPE)
        if status==0:
            blade.opstatus="successful"
            info("node shelf %s blade %s powered off succeed!" % (blade.shelf_id, blade.blade_id))
            return status, response
        else:
            blade.opstatus="failed"
            raise ExecutionException(
                "attemp to power off node shelf %s blade %s failed!" % (blade.shelf_id, blade.blade_id))

    def pxeboot(self,blade):
        toolname = self.gettoolname()
        cmdtpl = "{tool} {config} {shelf} {blade} {cmd}"
        OPE = cmdtpl.format(tool=toolname, config=self.configfile, shelf=blade.shelf_id, blade=blade.blade_id,
                              cmd="setpxe")
        blade.optype="pxeboot"
        status, response = SimpleCmd(OPE)
        if status==0:
            blade.opstatus="successful"
            info("node shelf %s blade %s pxeboot setup succeed!" % (blade.shelf_id, blade.blade_id))
            return status, response
        else:
            blade.opstatus="failed"
            raise ExecutionException(
                "attemp to pxeboot setup node shelf %s blade %s failed!" % (blade.shelf_id, blade.blade_id))

    def generatenodeinfo(self,blade):
        if self.checkexistence():
            toolname=self.gettoolname()
            cmdtpl = "{tool} {config} {shelf} {blade} {cmd}"
            OPE = cmdtpl.format(tool=toolname, config=self.configfile, shelf=blade.shelf_id, blade=blade.blade_id,
                                cmd="getsn")
            debuginfo("the OPE command is %s " % OPE)
            blade.optype="generatenodeinfo"
            status, response = SimpleCmd(OPE)
            if status == 0:
                debuginfo("the response is %s" % (response))
                with open('/var/lib/ericsson/node.info', 'a') as outf:
                    outf.write("%d %d %s\n" % (blade.shelf_id, blade.blade_id, response))
                blade.opstatus = "successful"
                info("node shelf %s blade %s %s succeed!" % (blade.shelf_id, blade.blade_id, blade.optype))
                return status, response
            else:
                blade.opstatus = "failed"
                raise ExecutionException(
                    " node shelf %s blade %s failed!" % (blade.shelf_id, blade.blade_id))

    def nicassignmentchk(self,blade):
        blade.optype="nicassignmentchk"
        if self.checkexistence():
            NIP=self._gettargetnip(blade)
            # generate embeded script
            scriptfilename = "/tmp/fetchPCIAddr.sh"
            self._createscript(scriptfilename)

            # transfer script to target node
            transfercmdtpl="scp -q {src} {dest}"
            transfercmd=transfercmdtpl.format(src=scriptfilename,dest="root@"+NIP+":"+scriptfilename)
            status,response=SimpleCmd(transfercmd)
            if status!=0:
                raise TransferException("File transferring has problem")

            # execute the script to fetch specified info
            remoteexeccmdtpl="ssh -q {ip} {cmd}"
            remotecmd=remoteexeccmdtpl.format(ip=NIP,cmd=("bash %s" % scriptfilename))
            status, response=SimpleCmd(remotecmd)
            if status == 0:
                collectbusinfo(blade, response)
        return status, response


class RemoteExecutionException(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return repr(self.msg)

class ExecutionException(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return repr(self.msg)


def collectbusinfo(node, response):
    strarr=response.splitlines()
    for item in strarr:
        reobj=re.search("([0-9:,a-z]+) (.*)", item)
        key=reobj.group(2)
        value=reobj.group(1)
        node.businfo[key]=value

class worker(threading.Thread):
    exitFlag=0
    def __init__(self, threadid, name, envchecker, qin, qout, tasklist):
        threading.Thread.__init__(self)
        self.threadid = threadid
        self.name = name
        self.qin = qin
        self.qout = qout
        self.envchecker=envchecker
        self.tasklist=tasklist

    def _matchtask(self,taskname):
        if taskname in self.tasklist:
            return True
        return False

    def processing(self):
        try:
            while not worker.exitFlag:
                # timeout=0.05 for tune application not use up all cpu tick to access queue
                # this is non-blocking structure.
                node=self.qin.get(timeout=0.05)
                if self._matchtask("ipconn"):
                    result, response=self.envchecker.checkbmcipconnectivity(node)
                    if result==0:
                        node.ipconnectivity=True
                    else:
                        node.ipconnectivity=False
                        node.response+=response
                if self._matchtask("ipmi"):
                    result,response=self.envchecker.checkbmcipmiaccount(node)
                    if result==0:
                        if re.search("error|fail", response) is None:
                            node.ipmiaccountstatus=True
                    else:
                        node.ipmiaccountstatus=False
                        node.response+=response
                if dbg:
                    debuginfo(node.response)
                #pdb.set_trace()
                if self._matchtask("businfo"):
                    result, response=self.envchecker.accessbusinfo(node)
                    if result==0:
                        collectbusinfo(node, response)
                if self._matchtask("hwi"):
                    result, response=self.envchecker.serverinfo(node)
                    if result==0:
                        node.hwi=response

                if self._matchtask("enablenic"):
                    result, response=self.envchecker.enablenic(node)
                    if result==0:
                        node.opstatus="latest enablenic operation succeeded. "
                        info("all nic %s in blade shelf %s blade %s are enabled. " % (str(node.tgtnics), node.shelf_id, node.blade_id))

                if self._matchtask("disablenic"):
                    result, response=self.envchecker.disablenic(node)
                    if result==0:
                        node.opstatus="latest disablenic operation succeeded. "
                        info("all nic %s in blade shelf %s blade %s are disabled. " % (str(node.tgtnics), node.shelf_id, node.blade_id))

                if self._matchtask("flashnic"):
                    result, response=self.envchecker.flashnic(node)
                    if result==0:
                        node.opstatus="latest flashnic operation succeeded. "
                        info("all nic %s in blade shelf %s blade %s are flashing. " % (str(node.tgtnics), node.shelf_id, node.blade_id))

                if self._matchtask("enableuid"):
                    result, response=self.envchecker.enableuid(node)
                    if result==0:
                        node.opstatus="latest enableuid operation succeeded. "
                        info("the uid locator led in blade shelf %s blade %s is light up. " % (node.shelf_id, node.blade_id))

                if self._matchtask("disableuid"):
                    result, response=self.envchecker.disableuid(node)
                    if result==0:
                        node.opstatus="latest disableuid operation succeeded. "
                        info("the uid locator led in blade shelf %s blade %s is black out. " % (node.shelf_id, node.blade_id))

                if self._matchtask("flashuid"):
                    result, response=self.envchecker.flashuid(node)
                    if result==0:
                        node.opstatus="latest flashuid operation succeeded. "
                        info("the uid locator led in blade shelf %s blade %s are flashing. " % (node.shelf_id, node.blade_id))

                if self._matchtask("poweron"):
                    result, response=self.envchecker.poweron(node)
                    if result==0:
                        node.opstatus="latest poweron operation succeeded. "
                        info("completed shelf %s blade %s power on. " % (node.shelf_id, node.blade_id))

                if self._matchtask("poweroff"):
                    result, response=self.envchecker.poweroff(node)
                    if result==0:
                        node.opstatus="latest poweroff operation succeeded. "
                        info("completed shelf %s blade %s power off. " % (node.shelf_id, node.blade_id))

                if self._matchtask("pxeboot"):
                    result, response=self.envchecker.pxeboot(node)
                    if result==0:
                        node.opstatus="latest pxeboot setup operation succeeded. "
                        info("completed shelf %s blade %s pxeboot setup. " % (node.shelf_id, node.blade_id))

                if self._matchtask("generatenodeinfo"):
                    result,response=self.envchecker.generatenodeinfo(node)
                    if result==0:
                        node.opstatus="latest generatenodeinfo operation succeeded. "
                        info(" completed shelf %s blade %s nodeinfo creation. " % ( node.shelf_id, node.blade_id))

                if self._matchtask("nicassignmentchk"):
                    result,response=self.envchecker.nicassignmentchk(node)
                    if result==0:
                        node.opstatus="nicassignmentchk operation succeeded. "
                        info(" completed shelf %s blade %s nicassignmentchk creation. " % ( node.shelf_id, node.blade_id))

                self.showup(node)
        except Queue.Empty:
            debuginfo("ignore empty")

    def run(self):
        debuginfo("Starting " + self.name)
        self.processing()
        debuginfo("Exiting " + self.name)

    def showup(self, node):
        print(("shelf %d, blade %d, bmc ip %s status: %s, ipmi status: %s, type: %s, operation stauts %s") % (
        node.shelf_id, node.blade_id, node.ip, node.ipconnectivity, node.ipmiaccountstatus, node.hwi, node.opstatus))


#class presenter(threading.Thread):
#    def __init__(self, qout):
#        threading.Thread.__init__(self,qout,nodecounter)
#        self.qout=qout
#        self.nodecounter=nodecounter

#    def showup(self):
#        counter=0
#        while not self.qout.empty() and counter<=self.nodecounter:
#            counter+=1
#            node=self.qout.get(timeout=0.05)
#            print(("shelf %d, blade %d, bmc ip status: %s, ipmi status: %s") % (
#            node.shelf_id, node.blade_id, node.ipconnectivity, node.ipmiaccountstatus))
#            #self.present(node)

#    def run(self):
#        self.showup()

def searchrolename(blade, businfo):
    i=0
    for ctrl in blade.control:
        if re.search(businfo,ctrl):
            return "control%d" % i
        i+=1
    i=0
    for da in blade.data:
        if re.search(businfo,da):
            return "data%d" % i
        i+=1
    i=0
    for stor in blade.storage:
        if re.search(businfo,stor):
            return "storage%d" % i
        i+=1
    raise InfoNotFoundException("unable to find role defined for the pci bus address %s. " % businfo)

def searchBusInfoByRolename(blade, rolename):
    if re.search("control([0-1])", rolename):
        reobj=re.search("control([0-1])", rolename)
        ctrl=blade.control[int(reobj.group(1))]
        for key, value in blade.businfo.iteritems():
            if ctrl==key:
                return key, value
    if re.search("data", rolename):
        reobj = re.search("data([0-1])", rolename)
        da = blade.data[int(reobj.group(1))]
        for key, value in blade.businfo.iteritems():
            if da==key:
                return key, value
    if re.search("storage", rolename):
        reobj = re.search("storage([0-1])", rolename)
        stor = blade.storage[int(reobj.group(1))]
        for key,value in blade.businfo.iteritems():
            if stor==key:
                return key, value

    raise InfoNotFoundException("unable to find businfo for rolename %s defined for shelf %s blade %s. " %
                                (rolename, blade.shelf_id, blade.blade_id))


def showStatus(envchecker):

    print("============ final state review ==============")

    for node in envchecker.nodes:
        print(("shelf %d, blade %03d, bmc ip %s status: %s, ipmi status: %s, type: %s, operation type: %s, operation status: %s") % (
        node.shelf_id, node.blade_id, node.ip, node.ipconnectivity, node.ipmiaccountstatus, node.hwi, node.optype, node.opstatus))
        if len(node.businfo) >0:
            if node.optype=="nicassignmentchk":
                for item in ["control0", "control1","data0", "data1", "storage0", "storage1"]:
                    debuginfo("control nic from config is %s" % str(node.control))
                    businfo, nicname=searchBusInfoByRolename(node, item)
                    print("\trolename: %s, \tbusinfo: %s, \tnicname: %s" % (item, businfo, nicname))
                #for key,value in node.businfo.iteritems():
                #    print("\tbusinfo: %s, nicname: %s, rolename: %s " % (key, value, searchrolename(node, key)))
            else:
                for key,value in node.businfo.iteritems():
                    print("\tbusinfo: %s, nicname: %s" % (key, value))



def createworker(count):
    header='Thread'
    threadList=[]
    for item in range(count):
        threadList.append(header+"-"+str(item))
    return threadList

def usage():
    pass

def main():

    #default value for key parameter #定义了关键参数的默认值
    tasklist=["ipconn","ipmi","hwi"]    #tasklist的默认值
    config_yaml="/mnt/cee_config/config.yaml"#config_yaml的路径
    os.system("rm -f /var/lib/ericsson/node.info")#执行rm -f /var/lib/ericsson/node.info的命令 删除node.info的文件,如果没有这个文件,就忽略且不显示信息

    parser = argparse.ArgumentParser(description='CEE Environment prechecker for large scale deployment.')#描述程序,把CEE Environment prechecker for large scale deployment.打印到屏幕上,ArgumentParser是个解析器对象
    parser.add_argument('taskarr', metavar="tasklist", nargs='+', help='task list for execution')
    parser.add_argument("--config", default="/mnt/cee_config/config.yaml", help="configuration file to load",#没有参数时从default中取值,dest是把位置或者选项关联到congif中
                        dest="config")
    parser.add_argument("--niclist", default="storage0,storage1", help="niclist presented by businfo for operation",
                        dest="niclist")

    args = parser.parse_args() #传递一组参数字符串来解析命令行
    #print parser.parse_args()



    #if len(sys.argv) == 2:
    #    config_yaml = sys.argv[1]
    #    if not os.path.isfile(config_yaml):
    #        sys.stderr.write("%s: Can not open %s\n\n" %
    #                     (sys.argv[0], config_yaml))
    #        usage()

    #if len(sys.argv) > 2:
    #    config_yaml = sys.argv[1]
    #    if not os.path.isfile(config_yaml):
    #        sys.stderr.write("%s: Can not open %s\n\n" %
    #                     (sys.argv[0], config_yaml))
    #        usage()
    tasklist = args.taskarr
    if args.config is not None:     #如果args的config属性不是空的话
        config_yaml=args.config     #就把args.config的值赋给config_yaml


    envchk = envchecker(config_yaml)    #把config_yaml作为环境变量检查这个类的参数传递过去,并把envchecker这个类的返回值传递给envchk

    workercount=len(envchk.nodes)     #envchk的nodes属性的长度传给workercount

    threadList = createworker(workercount)  #把workercount这个数字作为参数传递给createworker这个函数,并将值赋给threadList

    queueLock = threading.Lock()    #创建了一个线程锁对象queueLock
    inQueue = Queue.Queue(500)      #创建一个值为500的队列(FIFO)的对象inQueue   生产?
    outQueue=Queue.Queue(10)        #创建一个值为10的队列(FIFO)的对象outQueue  消费?
    threads = []                    #创建一个空list
    threadID = 1                    #将1赋值给threadID这个变量

    #pdb.set_trace()

    # Create new threads
    for tName in threadList:    #循环threadList
        thread = worker(threadID, tName, envchk, inQueue, outQueue, tasklist)       #将threadID, tName, envchk, inQueue, outQueue, tasklist函数传递给worker这个函数,这个函数的返回值传递给thread
        thread.start()          #开始线程的活动
        threads.append(thread)  #创建线程
        threadID += 1           #threadID自增


    # Fill the queue
    #queueLock.acquire()
    for node in envchk.nodes:
        inQueue.put(node)           #线程锁的释放
    #queueLock.release()

    # Wait for queue to empty
    while not inQueue.empty():
        pass

    # Notify threads it's time to exit
    worker.exitFlag = 1             #将1赋值给worker.exitFlag

    # Wait for all threads to complete
    for t in threads:               #结束线程
        t.join()

    showStatus(envchk)              #运行showStatus这个函数,参数时envchk,返回值是showStatus的返回值

    #presentThread.join()
    debuginfo("Exiting Main Thread")    #显示debuginfo信息


if __name__=="__main__":        #程序启动入口
    main()