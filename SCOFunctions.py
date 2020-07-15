import os
import json
import time
import requests
import threading
import traceback

import asyncio
import keyboard
import websockets

from MLogging import logclass
from ReplayAnalysis import analyse_replay


OverlayMessages = [] # Storage for all messages
lock = threading.Lock()
logger = logclass('SCOF','INFO')
initMessage = {'initEvent':True,'colors':['null','null','null','null'],'duration':60}
analysis_log_file = 'SCO_analysis_log.txt'
ReplayPosition = 0
AllReplays = dict()
PLAYER_NAMES = []


def set_initMessage(colors,duration,unifiedhotkey):
    """ Modify init message that's sent to new websockets """
    global initMessage
    initMessage['colors'] = colors
    initMessage['duration'] = duration
    initMessage['unifiedhotkey'] = 'true' if unifiedhotkey else 'false'


def sendEvent(event):
    """ Adds event to messages ready to be sent """
    with lock:
        OverlayMessages.append(event) 


def set_PLAYER_NAMES(names):
    """ Extends global player names variable """
    global PLAYER_NAMES
    with lock:
        PLAYER_NAMES.extend(names)


def guess_PLAYER_NAMES():
    """ If no PLAYER_NAMES are set, take the best guess for the preferred player"""
    global PLAYER_NAMES

    if len(PLAYER_NAMES) > 0:
        return 

    # Get all players to the list
    list_of_players = list()
    for replay in AllReplays:
        replay_dict = AllReplays[replay].get('replay_dict',dict())
        list_of_players.append(replay_dict.get('main',None))
        list_of_players.append(replay_dict.get('ally',None))

    # Remove nones, sort
    players = {i:list_of_players.count(i) for i in list_of_players if not i in [None,'None']} #get counts
    players = {k:v for k,v in sorted(players.items(),key=lambda x:x[1],reverse=True)} #sort
    logger.info(f'Guessing player names: {players}')

    if len(players) == 0:
        return

    # Get the three most common names. Replay analysis will check from the first one to the last one if they are ingame.
    with lock:
        PLAYER_NAMES = list(players.keys())[0:3] 


def initialize_AllReplays(ACCOUNTDIR):
    """ Creates a sorted dictionary of all replays with their last modified times """
    AllReplays = set()
    try:
        for root, directories, files in os.walk(ACCOUNTDIR):
            for file in files:
                if file.endswith('.SC2Replay'):
                    file_path = os.path.join(root,file)
                    if len(file_path) > 255:
                        file_path = '\\\?\\' + file_path
                    AllReplays.add(file_path)

        AllReplays = ((rep,os.path.getmtime(rep)) for rep in AllReplays)
        AllReplays = {k:{'created':v} for k,v in sorted(AllReplays,key=lambda x:x[1])}

        # Append data from already parsed replays
        if os.path.isfile(analysis_log_file):
            with open(analysis_log_file,'rb') as file:
                for line in file.readlines():
                    try:
                        replay_dict = eval(line.decode('utf-8'))
                        if 'replaydata' in replay_dict and 'filepath' in replay_dict:
                            if replay_dict['filepath'] in AllReplays:
                                AllReplays[replay_dict['filepath']]['replay_dict'] = replay_dict
                    except:
                        print("Failed to parse a line from replay analysis log\n",traceback.format_exc())

    except:
        logger.error(f'Error during replay initialization\n{traceback.format_exc()}')
    finally:
        return AllReplays


def check_replays(ACCOUNTDIR,AOM_NAME,AOM_SECRETKEY):
    """ Checks every few seconds for new replays """
    global AllReplays
    global ReplayPosition
    session_games = {'Victory':0,'Defeat':0}

    with lock:
        AllReplays = initialize_AllReplays(ACCOUNTDIR)
        logger.info(f'Initializing AllReplays with length: {len(AllReplays)}')
        ReplayPosition = len(AllReplays)

    guess_PLAYER_NAMES()

    while True:
        # Check for new replays
        current_time = time.time()
        for root, directories, files in os.walk(ACCOUNTDIR):
            for file in files:
                file_path = os.path.join(root,file)
                if len(file_path) > 255:
                    file_path = '\\\?\\' + file_path
                if file.endswith('.SC2Replay') and not(file_path in AllReplays): 
                    with lock:
                        AllReplays[file_path] = {'created':os.path.getmtime(file_path)}

                    if current_time - os.path.getmtime(file_path) < 60: 
                        logger.info(f'New replay: {file_path}')
                        replay_dict = dict()
                        try:   
                            replay_dict = analyse_replay(file_path,PLAYER_NAMES)

                            # Good output
                            if len(replay_dict) > 1:
                                logger.debug('Replay analysis result looks good, appending...')
                                session_games[replay_dict['result']] += 1                                    
                                    
                                sendEvent({**replay_dict,**session_games})
                                with open(analysis_log_file, 'ab') as file: #save into a text file
                                    file.write((str(replay_dict)+'\n').encode('utf-8'))     
                            # No output                         
                            else:
                                logger.error(f'ERROR: No output from replay analysis ({file})')

                            with lock:
                                AllReplays[file_path]['replay_dict'] = replay_dict
                                ReplayPosition = len(AllReplays)-1
                
                        except:
                            logger.error(traceback.format_exc())

                        finally:
                            upload_to_aom(file_path,AOM_NAME,AOM_SECRETKEY,replay_dict)
                            break

        time.sleep(3)   


def upload_to_aom(file_path,AOM_NAME,AOM_SECRETKEY,replay_dict):
    """ Function handling uploading the replay on the Aommaster's server"""

    # Credentials need to be set up
    if AOM_NAME == None or AOM_SECRETKEY == None:
        return

    # Never upload old replays
    if (time.time() - os.path.getmtime(file_path)) > 60:
        return

    # Upload only valid non-arcade replays
    if replay_dict.get('mainCommander',None) in [None,''] or '[MM]' in file_path:
        sendEvent({'uploadEvent':True,'response':'Not valid replay for upload'})
        return

    url = f'http://starcraft2coop.com/scripts/assistant/replay.php?username={AOM_NAME}&secretkey={AOM_SECRETKEY}'
    try:
        with open(file_path, 'rb') as file:
            response = requests.post(url, files={'file': file})
        logger.info(f'Replay upload reponse:\n{response.text}')
     
        if 'Success' in response.text or 'Error' in response.text:
            sendEvent({'uploadEvent':True,'response':response.text})
    
    except:
        sendEvent({'uploadEvent':True,'response':'Error'})
        logger.error(f'Failed to upload replay\n{traceback.format_exc()}')


async def manager(websocket, path):
    """ Manages websocket connection for each client """
    overlayMessagesSent = 0
    logger.info(f"STARTING WEBSOCKET: {websocket}")
    await websocket.send(json.dumps(initMessage))
    logger.info(f"Sending init message: {initMessage}")
    while True:
        try:
            if len(OverlayMessages) > overlayMessagesSent:
                message = json.dumps(OverlayMessages[overlayMessagesSent])
                logger.info(f'Sending message #{overlayMessagesSent} through {websocket}')
                overlayMessagesSent += 1
                await websocket.send(message)
        except websockets.exceptions.ConnectionClosedOK:
            logger.error('Websocket connection closed OK!')
            break
        except websockets.exceptions.ConnectionClosedError:
            logger.error('Websocket connection closed ERROR!')
            break            
        except websockets.exceptions.ConnectionClosed:
            logger.error('Websocket connection closed!')
            break 
        except:
            logger.error(traceback.format_exc())
        finally:
            await asyncio.sleep(0.1)


def server_thread(PORT):
    """ Creates a websocket server """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        start_server = websockets.serve(manager, 'localhost', PORT)
        logger.info('Starting websocket server')
        loop.run_until_complete(start_server)
        loop.run_forever()
    except:
        logger.error(traceback.format_exc())


def move_in_AllReplays(delta):
    """ Moves across all replays and sends info to overlay to show parsed data """
    global ReplayPosition
    logger.info(f'Attempt to move to {ReplayPosition + delta}/{len(AllReplays)-1}')

    # Check if valid position
    newPosition = ReplayPosition + delta
    if newPosition < 0 or newPosition >= len(AllReplays):
        logger.info(f'We have gone too far. Staying at {ReplayPosition}')
        return

    with lock:
        ReplayPosition = newPosition

    # Get replay_dict of given replay
    key = list(AllReplays.keys())[ReplayPosition]
    if 'replay_dict' in AllReplays[key]:
        if AllReplays[key]['replay_dict'] != None:
            sendEvent(AllReplays[key]['replay_dict'])
        else:
            logger.info(f"This replay couldn't be analysed {key}")
            move_in_AllReplays(delta)
    else:
        # Replay_dict is missing, analyse replay
        try: 
            replay_dict = analyse_replay(key,PLAYER_NAMES)
            if len(replay_dict) > 1:
                sendEvent(replay_dict)
                with lock:
                    AllReplays[key]['replay_dict'] = replay_dict
                with open(analysis_log_file, 'ab') as file:
                    file.write((str(replay_dict)+'\n').encode('utf-8'))  
            else:
                # No output from analysis
                with lock:
                    AllReplays[key]['replay_dict'] = None
                move_in_AllReplays(delta)
        except:
            logger.error(f'Failed to analyse replay: {key}\n{traceback.format_exc()}')
            with lock:
                AllReplays[key]['replay_dict'] = None
            move_in_AllReplays(delta)


def keyboard_thread_OLDER(OLDER):
    """ Thread waiting for hotkey for showing older replay"""
    logger.info('Starting keyboard older thread')
    while True:
        keyboard.wait(OLDER)
        move_in_AllReplays(-1)
        

def keyboard_thread_NEWER(NEWER):
    """ Thread waiting for hotkey for showing newer replay"""
    logger.info('Starting keyboard newer thread')
    while True:
        keyboard.wait(NEWER)
        move_in_AllReplays(1)


def keyboard_thread_HIDE(HIDE):
    """ Thread waiting for hide hotkey """
    logger.info('Starting keyboard hide thread')
    while True:
        keyboard.wait(HIDE)
        logger.info('Hide event')
        sendEvent({'hideEvent': True})


def keyboard_thread_SHOW(SHOW):
    """ Thread waiting for show hotkey """
    logger.info('Starting keyboard show thread')
    while True:
        keyboard.wait(SHOW)
        logger.info('Show event')
        sendEvent({'showEvent': True})