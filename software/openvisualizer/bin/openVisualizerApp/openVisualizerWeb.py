#!/usr/bin/python
# Copyright (c) 2013, Ken Bannister.
# All rights reserved.
#
# Released under the BSD 2-Clause license as published at the link below.
# http://opensource.org/licenses/BSD-2-Clause

import sys
import os

if __name__=="__main__":
    # Update pythonpath if running in in-tree development mode
    basedir  = os.path.dirname(__file__)
    confFile = os.path.join(basedir, "openvisualizer.conf")
    if os.path.exists(confFile):
        import pathHelper
        pathHelper.updatePath()

import logging
log = logging.getLogger('openVisualizerWeb')

try:
    from openvisualizer.moteState import moteState
except ImportError:
    # Debug failed lookup on first library import
    print 'ImportError: cannot find openvisualizer.moteState module'
    print 'sys.path:\n\t{0}'.format('\n\t'.join(str(p) for p in sys.path))

import json
import bottle
import re
import threading
import signal
import functools
import datetime
from bottle        import view, response

import openVisualizerApp
from openvisualizer.eventBus      import eventBusClient
from openvisualizer.SimEngine     import SimEngine
from openvisualizer.BspEmulator   import VcdLogger
from openvisualizer import ovVersion
from coap import coap
import time

# add default parameters to all bottle templates
view = functools.partial(view, ovVersion='.'.join(list([str(v) for v in ovVersion.VERSION])))

class OpenVisualizerWeb(eventBusClient.eventBusClient):
    '''
    Provides web UI for OpenVisualizer. Runs as a webapp in a Bottle web
    server.
    '''

    def __init__(self,app,websrv):
        '''
        :param app:    OpenVisualizerApp
        :param websrv: Web server
        '''
        log.info('Creating OpenVisualizerWeb')

        # store params
        self.app             = app
        self.engine          = SimEngine.SimEngine()
        self.websrv          = websrv
        self.roverMode       = app.roverMode
        self.ctrlMode        = app.ctrlMode
        self.simMode         = app.simulatorMode

        # define routes
        self._defineRoutes()

        # used for remote motes :
        if self.roverMode:
            self.roverMotes = {}
            self.client = coap.coap()
            self.client.respTimeout   = 2
            self.client.ackTimeout    = 2
            self.client.maxRetransmit = 1

        # used for controller mode
        if self.ctrlMode:
            self.openController = self.app.getOpenController()
            self.sm             = self.openController.getScheduleMgr()
            self.tm             = self.openController.getTopoMgr()
            self.startupConfig  = {}
            self._loadConfig()
            self.schedule = self.sm.getSchedule()

        # To find page templates
        bottle.TEMPLATE_PATH.append('{0}/web_files/templates/'.format(self.app.datadir))

        # initialize parent class
        eventBusClient.eventBusClient.__init__(
            self,
            name                  = 'OpenVisualizerWeb',
            registrations         =  [],
        )

        # Set DAGroots imported
        if app.DAGrootList:
            #Wait the end of the mote threads creation
            time.sleep(1)
            for moteid in app.DAGrootList:
                self._showMoteview(moteid)
                self._getMoteData(moteid)
                self._toggleDAGroot(moteid)


    #======================== public ==========================================

    #======================== private =========================================

    def _defineRoutes(self):
        '''
        Matches web URL to impelementing method. Cannot use @route annotations
        on the methods due to the class-based implementation.
        '''
        self.websrv.route(path='/',                                       callback=self._showMoteview)
        self.websrv.route(path='/moteview',                               callback=self._showMoteview)
        self.websrv.route(path='/moteview/:moteid',                       callback=self._showMoteview)
        self.websrv.route(path='/motedata/:moteid',                       callback=self._getMoteData)
        self.websrv.route(path='/toggleDAGroot/:moteid',                  callback=self._toggleDAGroot)
        if not self.simMode :
            self.websrv.route(path='/reset/:moteid',                      callback=self._reset)
        self.websrv.route(path='/eventBus',                               callback=self._showEventBus)
        self.websrv.route(path='/routing',                                callback=self._showRouting)
        self.websrv.route(path='/routing/dag',                            callback=self._showDAG)
        self.websrv.route(path='/eventdata',                              callback=self._getEventData)
        self.websrv.route(path='/wiresharkDebug/:enabled',                callback=self._setWiresharkDebug)
        self.websrv.route(path='/gologicDebug/:enabled',                  callback=self._setGologicDebug)
        self.websrv.route(path='/topology',                               callback=self._topologyPage)
        self.websrv.route(path='/topology/data',                          callback=self._topologyData)
        self.websrv.route(path='/topology/download',                      callback=self._topologyDownload)
        self.websrv.route(path='/topology/motes',         method='POST',  callback=self._topologyMotesUpdate)
        self.websrv.route(path='/topology/connections',   method='PUT',   callback=self._topologyConnectionsCreate)
        self.websrv.route(path='/topology/connections',   method='POST',  callback=self._topologyConnectionsUpdate)
        self.websrv.route(path='/topology/connections',   method='DELETE',callback=self._topologyConnectionsDelete)
        self.websrv.route(path='/topology/route',         method='GET',   callback=self._topologyRouteRetrieve)
        self.websrv.route(path='/static/<filepath:path>',                 callback=self._serverStatic)
        if self.roverMode:
            self.websrv.route(path='/rovers',                             callback=self._showrovers)
            self.websrv.route(path='/updateroverlist/:updatemsg',         callback=self._updateRoverList)
            self.websrv.route(path='/motesdiscovery/:srcip',              callback=self._motesDiscovery)
        if self.ctrlMode:
            self.websrv.route(path='/controller',                         callback=self._showController)
            self.websrv.route(path='/controller/upload',  method='POST',  callback=self._uploadschedule)
            self.websrv.route(path='/schedule/:cmd'    ,                  callback=self._schedule)
            self.websrv.route(path='/setbitmap/:bitmap',                  callback=self._setbitmap)
            self.websrv.route(path='/bier/:params',                       callback=self._bierparams)

    @view('controller.tmpl')
    def _showController(self, moteid = None):
        '''
            Handles the path calculation and resource management of the mesh network
        '''

        tmplData = {
            'roverMode': self.roverMode,
            'ctrlMode' : self.ctrlMode,
            'sim_mode' : self.simMode,
            'enable_bier' : self.app.getOpenLbr().getSendWithBier(),
            'auto_bier'   : self.app.getOpenLbr().getBierAuto(),
            'path_type'   : self.tm.getRepType()
        }
        return tmplData

    def _uploadschedule(self):
        '''
            Loads schedule from WebUI.
        '''
        data = bottle.request.forms.get('schedule')
        self._loadConfig(json.loads(data))
        return json.dumps(self.startupConfig)

    def _schedule(self, cmd):
        '''
            Manipulates schedule through commands.
        '''

        if  cmd == "install":
            self.sm.installSchedule(self.startupConfig)
            return json.dumps(self.sm.getRunningFrames())
        elif cmd == "clearbier":
            self.schedule.configFrame('clear')
            return json.dumps(self.sm.getRunningFrames())
        elif cmd == "clearshared":
            self.schedule.clearSharedSlots()
            return json.dumps(self.sm.getRunningFrames())
        elif cmd == "clearall":
            self.schedule.configFrame('clear')
            self.schedule.clearSharedSlots()
            return json.dumps(self.sm.getRunningFrames())
        elif cmd == "showrun":
            return json.dumps(self.sm.getRunningFrames())
        elif cmd == "showstartup":
            return json.dumps(self.startupConfig)
        elif cmd == "default":
            self._loadConfig()
            return json.dumps(self.startupConfig)
        else:
            return '{"result" : "failed"}'

    def _setbitmap(self, bitmap):
        '''
        Sets the openLbr bierbitmap to the one entered on the webUI

        :param bitmap: bitmap to set (as a string)
        '''
        try :
            int(bitmap, 2)
        except ValueError :
            log.error('A wrong bitmap was entered through webUI. It should only contain 0 and 1')
            return {"result" : "fail"}
        self.app.getOpenLbr().setBierBitmap(bitmap)
        return {"result" : "success"}

    def _bierparams(self, params):
        '''
        Enables or disabled the use of BIER to send messages

        :param params: string of params
        '''
        if params == 'on' :
            self.app.getOpenLbr().setSendWithBier(True)
            return {"result" : "success"}
        elif params == 'off' :
            self.app.getOpenLbr().setSendWithBier(False)
            return {"result" : "success"}
        elif params == 'autoon':
            self.app.getOpenLbr().setBierAuto(True)
            return {"result": "success"}
        elif params == 'autooff':
            self.app.getOpenLbr().setBierAuto(False)
            return {"result": "success"}
        elif params == 'singlepath':
            self.tm.setRepType(0)
            return {"result": "success"}
        elif params == 'dualpath':
            self.tm.setRepType(1)
            return {"result": "success"}
        elif params == 'fullpath':
            self.tm.setRepType(2)
            return {"result": "success"}
        else :
            return {"result": "fail"}

    def _loadConfig(self, config=None):
        '''
        loads the schedule if explicitly specified

        otherwise it loads the default configFile stored in schedule.json.
        '''

        if config:
            self.startupConfig = config
        else:
            try:
                with open('openvisualizer/openController/schedule.json') as json_file:
                    self.startupConfig = json.load(json_file)
            except IOError as err:
                log.debug("failed to load default startupSchedule. {0}".format(err))


    @view('rovers.tmpl')
    def _showrovers(self):
        '''
        Handles the discovery and connection to remote motes using remoteConnectorServer component
        '''
        import netifaces as ni
        myifdict = {}
        for myif in ni.interfaces():
            myifdict[myif] = ni.ifaddresses(myif)
        tmplData = {
            'myifdict'  : myifdict,
            'roverMotes' : self.roverMotes,
            'roverMode' : self.roverMode,
            'ctrlMode' : self.ctrlMode
        }
        return tmplData

    def _updateRoverList(self, updatemsg):
        '''
        Handles the devices discovery
        '''

        cmd, roverData = updatemsg.split('@')
        if cmd == "add":
            if roverData not in self.roverMotes.keys():
                self.roverMotes[roverData] = []
        elif cmd == "del":
            for roverIP in roverData.split(','):
                if roverIP in self.roverMotes.keys() and not self.roverMotes[roverIP]:
                    self.roverMotes.pop(roverIP)
        elif cmd == "upload":
            newRovers = roverData.split(",")
            for newRover in newRovers:
                if newRover not in self.roverMotes.keys():
                    self.roverMotes[newRover] = []
        elif cmd == "disconn":
            for roverIP in roverData.split(','):
                if roverIP in self.roverMotes.keys():
                    self.app.removeRoverMotes(roverIP, self.roverMotes.pop(roverIP))
        moteDict = self.app.getMoteDict()
        for rover in self.roverMotes:
            for i, serial in enumerate(self.roverMotes[rover]):
                for moteID, connserial in moteDict.items():
                    if serial == connserial:
                        self.roverMotes[rover][i] = moteID

        return json.dumps(self.roverMotes)

    def _motesDiscovery(self, srcip):
        '''
        Collects the list of motes available on the rover and connects them to oV
        Use connetest to first check service availability
        :param roverIP: IP of the rover
        '''
        coapThreads = []
        for roverip in self.roverMotes.keys():
            t = threading.Thread(target=self._getCoapResponse, args=(srcip, roverip))
            t.setDaemon(True)
            t.start()
            coapThreads.append(t)
        for t in coapThreads:
            t.join()
        self.app.refreshRoverMotes(self.roverMotes)
        return json.dumps(self.roverMotes)

    def _getCoapResponse(self, srcip, roverip):
        log.info("sending coap request to rover {0}".format(roverip))
        try:
            if ':' in roverip:
                response = self.client.PUT('coap://[{0}]/pcinfo'.format(roverip),
                                           payload=[ord(c) for c in (srcip + ';50000;' + roverip)])
            else:
                response = self.client.PUT('coap://{0}/pcinfo'.format(roverip),
                                           payload=[ord(c) for c in (srcip + ';50000;' + roverip)])
            payload = ''.join([chr(b) for b in response])
            self.roverMotes[roverip] = json.loads(payload)
            self.roverMotes[roverip] = [rm + '@' + roverip for rm in self.roverMotes[roverip]]
        except Exception as err:
            self.roverMotes[roverip] = str(err)

    @view('moteview.tmpl')
    def _showMoteview(self, moteid=None):
        '''
        Collects the list of motes, and the requested mote to view.

        :param moteid: 16-bit ID of mote (optional)
        '''
        log.debug("moteview moteid parameter is {0}".format(moteid))

        motelist = self.app.getMoteDict().keys()

        tmplData = {
            'motelist'       : motelist,
            'requested_mote' : moteid if moteid else 'none',
            'roverMode'      : self.roverMode,
            'ctrlMode'       : self.ctrlMode,
            'sim_mode'       : self.simMode
        }
        return tmplData

    def _serverStatic(self, filepath):
        return bottle.static_file(filepath,
                                  root='{0}/web_files/static/'.format(self.app.datadir))

    def _toggleDAGroot(self, moteid):

        '''
        Triggers toggle DAGroot state, via moteState. No real response. Page is
        updated when next retrieve mote data.
        :param moteid: 16-bit ID of mote
        '''

        log.info('Toggle root status for moteid {0}'.format(moteid))
        ms = self.app.getMoteState(moteid)
        if ms:
            log.debug('Found mote {0} in moteStates'.format(moteid))
            ms.triggerAction(ms.TRIGGER_DAGROOT)
            return '{"result" : "success"}'
        else:
            log.debug('Mote {0} not found in moteStates'.format(moteid))
            return '{"result" : "fail"}'

    def _reset(self, moteid):
        '''
        Resets a mote. No real response. Page is updated when next retrieve mote
        data.
        :param moteid: 16-bit ID of mote
        '''
        returnval = '{"result" : "success"}'
        if moteid == "all":
            motelist = self.app.getMoteDict().keys()
        else:
            motelist = [moteid]
        for mote in motelist:
            log.info('Reset moteid {0}'.format(mote))
            ms = self.app.getMoteState(mote)
            if ms:
                log.debug('Found mote {0} in moteStates'.format(mote))
                ms.triggerAction(ms.RESET)
            else:
                log.debug('Mote {0} not found in moteStates'.format(mote))
                returnval = '{"result" : "fail"}'
        return returnval

    def _getMoteData(self, moteid):
        '''
        Collects data for the provided mote.

        :param moteid: 16-bit ID of mote
        '''
        log.debug('Get JSON data for moteid {0}'.format(moteid))
        ms = self.app.getMoteState(moteid)
        if ms:
            log.debug('Found mote {0} in moteStates'.format(moteid))
            states = {
                ms.ST_IDMANAGER   : ms.getStateElem(ms.ST_IDMANAGER).toJson('data'),
                ms.ST_ASN         : ms.getStateElem(ms.ST_ASN).toJson('data'),
                ms.ST_ISSYNC      : ms.getStateElem(ms.ST_ISSYNC).toJson('data'),
                ms.ST_MYDAGRANK   : ms.getStateElem(ms.ST_MYDAGRANK).toJson('data'),
                ms.ST_KAPERIOD    : ms.getStateElem(ms.ST_KAPERIOD).toJson('data'),
                ms.ST_OUPUTBUFFER : ms.getStateElem(ms.ST_OUPUTBUFFER).toJson('data'),
                ms.ST_BACKOFF     : ms.getStateElem(ms.ST_BACKOFF).toJson('data'),
                ms.ST_MACSTATS    : ms.getStateElem(ms.ST_MACSTATS).toJson('data'),
                ms.ST_SCHEDULE    : ms.getStateElem(ms.ST_SCHEDULE).toJson('data'),
                ms.ST_QUEUE       : ms.getStateElem(ms.ST_QUEUE).toJson('data'),
                ms.ST_NEIGHBORS   : ms.getStateElem(ms.ST_NEIGHBORS).toJson('data'),
            }
        else:
            log.debug('Mote {0} not found in moteStates'.format(moteid))
            states = {}
        return states

    def _setWiresharkDebug(self, enabled):
        '''
        Selects whether eventBus must export debug packets.

        :param enabled: 'true' if enabled; any other value considered false
        '''
        log.info('Enable wireshark debug : {0}'.format(enabled))
        self.app.eventBusMonitor.setWiresharkDebug(enabled == 'true')
        return '{"result" : "success"}'

    def _setGologicDebug(self, enabled):
        log.info('Enable GoLogic debug : {0}'.format(enabled))
        VcdLogger.VcdLogger().setEnabled(enabled == 'true')
        return '{"result" : "success"}'

    @view('eventBus.tmpl')
    def _showEventBus(self):
        '''
        Simple page; data for the page template is identical to the data
        for periodic updates of event list.
        '''
        tmplData = self._getEventData().copy()
        tmplData['roverMode'] = self.roverMode
        tmplData['ctrlMode'] = self.ctrlMode
        return tmplData

    def _showDAG(self):
        states,edges = self.app.topology.getDAG()
        return { 'states': states, 'edges': edges }

    @view('routing.tmpl')
    def _showRouting(self):
        return {'roverMode' : self.roverMode, 'ctrlMode' : self.ctrlMode}

    @view('topology.tmpl')
    def _topologyPage(self):
        '''
        Retrieve the HTML/JS page.
        '''

        return {'roverMode' : self.roverMode, 'ctrlMode' : self.ctrlMode}

    def _topologyData(self):
        '''
        Retrieve the topology data, in JSON format.
        '''

        # motes
        motes = []
        rank  = 0
        while True:
            try:
                mh            = self.engine.getMoteHandler(rank)
                id            = mh.getId()
                (lat,lon)     = mh.getLocation()
                motes += [
                    {
                        'id':    id,
                        'lat':   lat,
                        'lon':   lon,
                    }
                ]
                rank+=1
            except IndexError:
               break

        # connections
        connections = self.engine.propagation.retrieveConnections()

        data = {
            'motes'          : motes,
            'connections'    : connections,
        }

        return data

    def _topologyMotesUpdate(self):

        motesTemp = {}
        for (k,v) in bottle.request.forms.items():
            m = re.match("motes\[(\w+)\]\[(\w+)\]", k)
           
            assert m
            index  = int(m.group(1))
            param  =     m.group(2)
            try:
                v  = int(v)
            except ValueError:
                try:
                    v  = float(v)
                except ValueError:
                    pass
            if index not in motesTemp:
                motesTemp[index] = {}
            motesTemp[index][param] = v

        for (_,v) in motesTemp.items():
            mh = self.engine.getMoteHandlerById(v['id'])
            mh.setLocation(v['lat'],v['lon'])

    def _topologyConnectionsCreate(self):

        data = bottle.request.forms
        assert sorted(data.keys())==sorted(['fromMote', 'toMote'])

        fromMote = int(data['fromMote'])
        toMote   = int(data['toMote'])

        self.engine.propagation.createConnection(fromMote,toMote)

    def _topologyConnectionsUpdate(self):
        data = bottle.request.forms
        assert sorted(data.keys())==sorted(['fromMote', 'toMote', 'pdr'])

        fromMote = int(data['fromMote'])
        toMote   = int(data['toMote'])
        pdr      = float(data['pdr'])

        self.engine.propagation.updateConnection(fromMote,toMote,pdr)

    def _topologyConnectionsDelete(self):

        data = bottle.request.forms
        assert sorted(data.keys())==sorted(['fromMote', 'toMote'])

        fromMote = int(data['fromMote'])
        toMote   = int(data['toMote'])

        self.engine.propagation.deleteConnection(fromMote,toMote)

    def _topologyRouteRetrieve(self):

        data = bottle.request.query

        assert data.keys()==['destination']

        detination_eui = [0x14,0x15,0x92,0xcc,0x00,0x00,0x00,int(data['destination'])]

        route = self._dispatchAndGetResult(
            signal       = 'getSourceRoute',
            data         = detination_eui,
        )

        route = [r[-1] for r in route]

        data = {
            'route'          : route,
        }

        return data

    def _topologyDownload(self):
        '''
        Retrieve the topology data, in JSON format, and download it.
        '''
        data = self._topologyData()
        now = datetime.datetime.now()
        DAGrootList=[]

        for ms in self.app.moteStates:
            if ms.getStateElem(moteState.moteState.ST_IDMANAGER).isDAGroot:
                DAGrootList.append(ms.getStateElem(moteState.moteState.ST_IDMANAGER).get16bAddr()[1])

        data['DAGrootList']=DAGrootList

        response.headers['Content-disposition']='attachement; filename=topology_data_'+now.strftime("%d-%m-%y_%Hh%M")+'.json'
        response.headers['filename']='test.json'
        response.headers['Content-type']= 'application/json'

        return data

    def _getEventData(self):
        response = {
            'isDebugPkts' : 'true' if self.app.eventBusMonitor.wiresharkDebugEnabled else 'false',
            'stats'       : self.app.eventBusMonitor.getStats(),
        }
        return response

#============================ main ============================================
from argparse       import ArgumentParser

def _addParserArgs(parser):
    '''Adds arguments specific to web UI.'''

    parser.add_argument('-H', '--host',
        dest       = 'host',
        default    = '0.0.0.0',
        action     = 'store',
        help       = 'host address'
    )

    parser.add_argument('-p', '--port',
        dest       = 'port',
        default    = 8080,
        action     = 'store',
        help       = 'port number'
    )


webapp = None
if __name__=="__main__":
    parser   =  ArgumentParser()
    _addParserArgs(parser)
    argspace = parser.parse_known_args()[0]

    # log
    log.info(
        'Initializing OpenVisualizerWeb with options: \n\t{0}'.format(
            '\n    '.join(
                [
                    'host = {0}'.format(argspace.host),
                    'port = {0}'.format(argspace.port)
                ]
            )
        )
    )

    #===== start the app
    app      = openVisualizerApp.main(parser)
    
    #===== add a web interface
    websrv   = bottle.Bottle()
    webapp   = OpenVisualizerWeb(app, websrv)


    # start web interface in a separate thread
    webthread = threading.Thread(
        target = websrv.run,
        kwargs = {
            'host'          : argspace.host,
            'port'          : argspace.port,
            'quiet'         : not app.debug,
            'debug'         : app.debug,
        }
    )
    webthread.start()
    
    #===== add a cli (minimal) interface

    banner  = []
    banner += ['OpenVisualizer']
    banner += ['web interface started at {0}:{1}'.format(argspace.host,argspace.port)]
    banner += ['enter \'q\' to exit']
    banner  = '\n'.join(banner)
    print banner

    while True:
        input = raw_input('> ')
        if input=='q':
            print 'bye bye.'
            app.close()
            os.kill(os.getpid(), signal.SIGTERM)
