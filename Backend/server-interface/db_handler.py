# Copyright 2016 The Eyra Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# File author/s:
#     Matthias Petursson <oldschool01123@gmail.com>
#     Robert Kjaran <robert@kjaran.com> (getRecordingsInfo)

from flask_mysqldb import MySQL
from MySQLdb import Error as MySQLError
import json
import os
import random

from util import log, filename
from config import dbConst, RECSURL

class DbHandler:
    def __init__(self, app):
        # MySQL configurations
        app.config['MYSQL_HOST'] = dbConst['host']
        app.config['MYSQL_USER'] = dbConst['user']
        app.config['MYSQL_DB']   = dbConst['db']
        app.config['MYSQL_USE_UNICODE'] = dbConst['use_unicode']
        app.config['MYSQL_CHARSET'] = dbConst['charset']

        self.mysql = MySQL(app)

        # path to saved recordings
        self.recordings_path = app.config['MAIN_RECORDINGS_PATH']

        # needed to sanitize the dynamic sql creation in insertGeneralData
        # keep a list of allowed column names for insertions etc. depending on the table (device, isntructor, etc)
        self.allowedColumnNames = {
            'device': [
                'userAgent',
                'imei'
            ],
            'instructor': [
                'name',
                'email',
                'phone',
                'address'
            ],
            'speaker': [
                'name',
                'deviceImei'
            ],
            'speaker_info': [
                'speakerId',
                's_key',
                's_value'
            ]
        }

        # generate list of currently valid tokens according to 'valid' column in table token.
        #self.invalid_token_ids = self.getInvalidTokenIds() # messes up WSGI script for some fucking reason
        self.invalid_token_ids = None

    def getInvalidTokenIds(self):
        """
        Returns a list of tokenId's who are marked with valid=FALSE in database.
        """
        cur = self.mysql.connection.cursor()
        cur.execute('SELECT id FROM token WHERE valid=FALSE')
        return [row[0] for row in cur.fetchall()]

    def insertGeneralData(self, name, data, table):
        """
        inserts data into appropriate table

        name is i.e. 'instructor' and is a representation of the data, for errors and general identification
        data is a json object whose keys will be used as table column names and those values
        will be inserted into table
        returns the id of the newly inserted row or errors in the format
        dict(msg=id or msg, statusCode=htmlStatusCode)

        Example:
        name='device'
        data = {'imei':245, 'userAgent':'Mozilla'}
        table = 'device'

        In which case, this function will 
        insert into device (imei, userAgent) 
                values ('245','Mozilla')
        and return said rows newly generated id.

        WARNING: appends the keys of data straight into a python string using %
          so at least this should be sanitized. Sanitized by a whitelist of
          allowed keys in self.allowedColumnNames
        """
        keys = []
        vals = []
        dataId = None
        try:
            if isinstance(data, str):
                data = json.loads(data)

            for key, val in data.items(): # use data.iteritems() for python 2.7
                # allow only keys from the appropriate list in self.allowedColumnNames
                if key not in self.allowedColumnNames[name]:
                    raise KeyError('Unallowed column name used! Did someone hack the frontend? name: %s' % key)
                keys.append(key)
                vals.append(val)

            data = None # data is untrusted, should not be used unless it's filtered
        except (KeyError, TypeError, ValueError) as e:
            msg = '%s data not on correct format, aborting.' % name
            log(msg, e)
            return dict(msg=msg, statusCode=400)

        try: 
            # insert into table
            cur = self.mysql.connection.cursor()

            # make our query something like (with 4 key/value pairs)
            # 'INSERT INTO %s (%s, %s, %s, %s) \
            #              VALUES (%s, %s, %s, %s)',
            # depending on number of data keys/values 
            queryStr = 'INSERT INTO %s ('

            queryStrMid = '' # since we can reuse the (%s,%s,...)
            for i in range(len(keys)):
                queryStrMid += '%s'
                if (i != len(keys) - 1):
                    queryStrMid += ', '

            queryStr += queryStrMid
            queryStr += ') '

            # input the keys first, because we don't want the '' quotes that cur.execute
            #   automatically puts there
            queryStr = queryStr % tuple([table] + keys)

            queryStr += 'VALUES ('
            queryStr += queryStrMid
            queryStr += ')'

            # make the replacement tuple which is set in place of the %s's in the query
            queryTuple = tuple(vals)

            cur.execute(queryStr, queryTuple)

            # get the newly auto generated id

            # create our query something like
            # 'SELECT id FROM %s WHERE \
            #              %s=%s AND %s=%s AND %s=%s AND %s=%s'
            # but now the order is WHERE key=val AND key1=val1 and so
            # we have to interleave our lists instead of appending them 
            # to get the correct order 
            interleavedList = []
            for i in range(len(keys)):
                interleavedList.append(keys[i])
                # just a hack, because of the quote thing mentioned above
                #   will be replaces with vals in query
                interleavedList.append('%s') 
            
            queryStr = 'SELECT id FROM %s WHERE '
            for i in range(len(keys)):
                queryStr += '%s=%s'
                if (i != len(keys) - 1):
                    queryStr += ' AND '

            queryStr = queryStr % tuple([table] + interleavedList)

            cur.execute(queryStr, queryTuple)
            # return highest id in case of multiple results (should be the newest entry)
            dataIds = cur.fetchall()
            dataId = max([i[0] for i in dataIds]) # fetchall() returns a list of tuples

            # only commit if we had no exceptions until this point
            self.mysql.connection.commit()

        except MySQLError as e:
            msg = 'Database error.'
            log(msg, e)
            return dict(msg=msg, statusCode=500)

        if dataId is None:
            msg = 'Couldn\'t get %s id.' % name
            log(msg)
            return dict(msg=msg, statusCode=500)
        else:
            return dict(msg='{"%sId":' % name + str(dataId) + '}', statusCode=200)

    def insertSpeakerData(self, speakerData, speakerInfo):
        """
        inserts into both speaker and speaker_info
        speakerData is the {'name':name[, 'deviceImei':deviceImei]}
        speakerInfo are the extra info values to insert into
          speaker_info table, e.g. speakerInfo: {'height':'154', etc.}
        assumes speaker doesn't exist in database.
        """
        speakerId = None
        res = self.insertGeneralData('speaker', speakerData, 'speaker')
        if 'speakerId' in res['msg']:
            speakerId = json.loads(res['msg'])['speakerId']
        else:
            return res
        for k, v in speakerInfo.items():
            self.insertGeneralData('speaker_info', {
                                                        'speakerId':speakerId,
                                                        's_key':k,
                                                        's_value':v
                                                    },
                                    'speaker_info')
        return res

    def processInstructorData(self, instructorData):
        """
        instructorData = look at format in the client-server API
        """
        try:
            if isinstance(instructorData, str):
                instructorData = json.loads(instructorData)
        except (ValueError) as e:
            msg = '%s data not on correct format, aborting.' % name
            log(msg, e)
            return dict(msg=msg, statusCode=400)

        if 'id' in instructorData:
            # instructor was submitted as an id, see if he exists in database
            try: 
                cur = self.mysql.connection.cursor()

                cur.execute('SELECT id FROM instructor WHERE id=%s', (instructorData['id'],)) # have to pass in a tuple, with only one parameter
                instructorId = cur.fetchone()
                if (instructorId is None):
                    # no instructor
                    msg = 'No instructor with that id.'
                    log(msg)
                    return dict(msg=msg, statusCode=400)
                else:
                    # instructor already exists, return it
                    instructorId = instructorId[0] # fetchone returns tuple on success
                    return dict(msg='{"instructorId":' + str(instructorId) + '}', statusCode=200)
            except MySQLError as e:
                msg = 'Database error.'
                log(msg, e)
                return dict(msg=msg, statusCode=500)
            return 'Unexpected error.', 500

        return self.insertGeneralData('instructor', instructorData, 'instructor')

    def processDeviceData(self, deviceData):
        # we have to make sure not to insert device with same IMEI
        #   as is already in the database if so. Otherwise, we create new device
        deviceImei, deviceId, userAgent = None, None, None
        try:
            if isinstance(deviceData, str):
                deviceData = json.loads(deviceData)
            userAgent = deviceData['userAgent']
        except (TypeError, ValueError, KeyError) as e:
            msg = 'Device data not on correct format, aborting.'
            log(msg, e)
            return dict(msg=msg, statusCode=400)

        try:
            deviceImei = deviceData['imei']
        except (KeyError) as e:
            # we don't care if device has no ['imei']
            pass
        try:
            deviceId = deviceData['deviceId']
            del deviceData['deviceId'] # delete it, we don't want to insert it into database
        except (KeyError) as e:
            # we don't care if device has no ['deviceId']
            pass

        if deviceImei is not None and deviceImei != '':
            try: 
                cur = self.mysql.connection.cursor()

                # firstly, check if this device already exists, if so, update end time, otherwise add device
                cur.execute('SELECT id FROM device WHERE imei=%s', (deviceImei,)) # have to pass in a tuple, with only one parameter
                dbDeviceId = cur.fetchone()
                if (dbDeviceId is None):
                    # no device with this imei in database, insert it
                    return self.insertGeneralData('device', deviceData, 'device')
                else:
                    # device already exists, return it
                    dbDeviceId = dbDeviceId[0] # fetchone returns tuple on success
                    return dict(msg='{"deviceId":' + str(dbDeviceId) + '}', statusCode=200)
            except MySQLError as e:
                msg = 'Database error.'
                log(msg, e)
                return dict(msg=msg, statusCode=500)

        # no imei present, won't be able to identify device unless he has his id
        if deviceId is not None and deviceId != '':
            # check if a device with this id has the same userAgent as our devicedata
            try: 
                cur = self.mysql.connection.cursor()

                cur.execute('SELECT userAgent FROM device WHERE \
                             id=%s', (deviceId,))
                dbUserAgent = cur.fetchone()
                if dbUserAgent is None:
                    # no device with this info in database, insert it
                    return self.insertGeneralData('device', deviceData, 'device')
                else:
                    # device already exists, check if names match
                    dbUserAgent = dbUserAgent[0]
                    if dbUserAgent == userAgent:
                        return dict(msg='{"deviceId":' + str(deviceId) + '}', statusCode=200)
                    else:
                        msg = 'userAgents don\'t match for supplied id. Creating new device.'
                        log(msg)
                        return self.insertGeneralData('device', deviceData, 'device')
            except MySQLError as e:
                msg = 'Database error.'
                log(msg, e)
                return dict(msg=msg, statusCode=500)

        # no id and no imei, must be new device and first transmission
        return self.insertGeneralData('device', deviceData, 'device')

    def processSpeakerData(self, speakerData):
        name, deviceImei, speakerId = None, None, None
        try:
            if isinstance(speakerData, str):
                speakerData = json.loads(speakerData)
            name = speakerData['name']
        except (KeyError, TypeError, ValueError) as e:
            msg = 'Speaker data not on correct format, aborting.'
            log(msg, e)
            return dict(msg=msg, statusCode=400)
        try:
            deviceImei = speakerData['deviceImei']
        except (KeyError) as e:
            # we don't care if speaker has no ['imei']
            pass
        try:
            speakerId = speakerData['speakerId']
        except (KeyError) as e:
            # or if he doesn't have an id
            pass

        # now, lets process the dynamic keys/values from speaker data
        # ignore name, speakerId and deviceImei keys from dict
        speakerInfo = {}
        for k, v in speakerData.items():
            if k != 'name' and k != 'deviceImei' and k != 'speakerId':
                speakerInfo[str(k)] = str(v)
        # recreate our speakerData object ready to store in db
        newSpeakerData = {'name':name}

        # if the speaker has imei info, use that to identify him
        if deviceImei is not None and deviceImei != '':
            newSpeakerData['deviceImei'] = deviceImei
            try: 
                cur = self.mysql.connection.cursor()

                # firstly, check if this speaker already exists, if so, return speakerId, otherwise add speaker
                cur.execute('SELECT id FROM speaker WHERE \
                         name=%s AND deviceImei=%s',
                        (name, deviceImei))
                dbSpeakerId = cur.fetchone()
                if (dbSpeakerId is None):
                    # no speaker with this info in database, insert it
                    return self.insertSpeakerData(newSpeakerData, speakerInfo)
                else:
                    # speaker already exists, return it
                    dbSpeakerId = dbSpeakerId[0] # fetchone returns tuple on success
                    return dict(msg='{"speakerId":' + str(dbSpeakerId) + '}', statusCode=200)
            except MySQLError as e:
                msg = 'Database error.'
                log(msg, e)
                return dict(msg=msg, statusCode=500)

        # no imei present, won't be able to identify speaker unless he has his id
        if speakerId is not None and speakerId != '':
            # check if a speaker with this id has the same name as our speakerdata
            try: 
                cur = self.mysql.connection.cursor()

                cur.execute('SELECT name FROM speaker WHERE \
                             id=%s', (speakerId,))
                dbName = cur.fetchone()
                if dbName is None:
                    # no speaker with this info in database, insert it
                    return self.insertSpeakerData(newSpeakerData, speakerInfo)
                else:
                    # speaker already exists, check if names match
                    dbName = dbName[0] # fetchone() returns a tuple
                    if dbName == name:
                        return dict(msg='{"speakerId":' + str(speakerId) + '}', statusCode=200)
                    else:
                        msg = 'Names don\'t match for supplied id. Creating new speaker.'
                        log(msg)
                        return self.insertSpeakerData(newSpeakerData, speakerInfo)
            except MySQLError as e:
                msg = 'Database error.'
                log(msg, e)
                return dict(msg=msg, statusCode=500)

        # no id and no imei, must be new speaker and first transmission
        return self.insertSpeakerData(newSpeakerData, speakerInfo)

    def processSessionData(self, jsonData, recordings):
        """
        Processes session data sent from client, saves it to the appropriate tables
        in the database, and saves the recordings to the filesystem at
        '<app.config['MAIN_RECORDINGS_PATH']>/session_<sessionId>/recname'

        parameters:
            jsonData        look at format in the client-server API
            recordings      an array of file objects representing the submitted recordings
        
        returns a dict (msg=msg, statusCode=200,400,..)
        msg on format: dict(deviceId=dId, speakerId=sId, sessionId=sesId, recsDelivered=numRecsInDb)
        """
        jsonDecoded = None
        sessionId = None
        # can be a number of messages, depending on the error.
        # sent back to the user, and used as a flag to see
        # if recordings should be saved but not put in mysqldb
        error = '' 
        errorStatusCode = 400 # modified if something else

        # vars from jsonData
        speakerId, instructorId, deviceId, location, start, end, comments = \
            None, None, None, None, None, None, None
        speakerName = None

        if type(recordings)!=list or len(recordings)==0:
            msg = 'No recordings received, aborting.'
            log(msg)
            return dict(msg=msg, statusCode=400)

        # extract json data
        try:
            jsonDecoded = json.loads(jsonData)
            #log(jsonDecoded)
     
            if jsonDecoded['type'] == 'session':
                jsonDecoded = jsonDecoded['data']
                speakerName = jsonDecoded['speakerInfo']['name']
                # this inserts speaker into database
                speakerId = json.loads(
                                self.processSpeakerData(
                                    jsonDecoded['speakerInfo']
                                )['msg']
                            )['speakerId']
                instructorId = jsonDecoded['instructorId']
                # this inserts device into database
                deviceId =  json.loads(
                                self.processDeviceData(
                                    jsonDecoded['deviceInfo']
                                )['msg']
                            )['deviceId']
                location = jsonDecoded['location']
                start = jsonDecoded['start']
                end = jsonDecoded['end']
                comments = jsonDecoded['comments']
            else:
                error = 'Wrong type of data.'
                log(error)
        except (KeyError, TypeError, ValueError) as e:
            error = 'Session data not on correct format.'
            log(error, e)

        if not error:
            try:
                # insert into session
                cur = self.mysql.connection.cursor()

                # firstly, check if this session already exists, if so, update end time, otherwise add session
                cur.execute('SELECT id FROM session WHERE \
                             speakerId=%s AND instructorId=%s AND deviceId=%s AND location=%s AND start=%s',
                            (speakerId, instructorId, deviceId, location, start))
                sessionId = cur.fetchone()
                if sessionId is None:
                    # create new session entry in database
                    cur.execute('INSERT INTO session (speakerId, instructorId, deviceId, location, start, end, comments) \
                                 VALUES (%s, %s, %s, %s, %s, %s, %s)', 
                                (speakerId, instructorId, deviceId, location, start, end, comments))
                    # get the newly auto generated session.id 
                    cur.execute('SELECT id FROM session WHERE \
                                 speakerId=%s AND instructorId=%s AND deviceId=%s AND location=%s AND start=%s AND end=%s',
                                (speakerId, instructorId, deviceId, location, start, end))
                    sessionId = cur.fetchone()[0] # fetchone returns a tuple
                else:
                    # session already exists, simply update end-time
                    sessionId = sessionId[0] # fetchone() returns tuple
                    cur.execute('UPDATE session \
                                 SET end=%s \
                                 WHERE id=%s', 
                                (end, sessionId))
            except MySQLError as e:
                error = 'Error inserting sessionInfo into database.'
                errorStatusCode = 500
                log(error, e)

        try:
            # now populate recordings table and save recordings+extra data to file/s

            # make sure path to recordings exists
            os.makedirs(self.recordings_path, exist_ok=True)

            for rec in recordings:
                # grab token to save as extra metadata later, and id to insert into table recording
                tokenId = jsonDecoded['recordingsInfo'][rec.filename]['tokenId']
                # use token sent as text if available to write to metadata file (in case database is wrong)
                #   to be salvaged later if needed.
                token = None
                if 'tokenText' in jsonDecoded['recordingsInfo'][rec.filename]:
                    token = jsonDecoded['recordingsInfo'][rec.filename]['tokenText']
                else:
                    # otherwise, grab it from the database
                    cur.execute('SELECT inputToken FROM token WHERE id=%s', (tokenId,))
                    token = cur.fetchone()
                    if token is None:
                        error = 'No token with supplied id.'
                        log(error.replace('id.','id: {}.'.format(tokenId)))
                    else:
                        token = token[0] # fetchone() returns tuple

                if not error:
                    recName = self.writeRecToFilesystem(rec, token, sessionId, speakerName, lost=False)
                else:
                    recName = self.writeRecToFilesystem(rec, token, sessionId, speakerName, lost=True)

                if not error:
                    # insert recording data into database
                    cur.execute('INSERT INTO recording (tokenId, speakerId, sessionId, filename) \
                                 VALUES (%s, %s, %s, %s)', 
                                (tokenId, speakerId, sessionId, recName))
        except MySQLError as e:
            msg = 'Error adding recording to database.'
            log(msg, e)
            return dict(msg=msg, statusCode=500)
        except os.error as e:
            msg = 'Error saving recordings to file.'
            log(msg, e)
            return dict(msg=msg, statusCode=500)
        except KeyError as e:
            msg = 'Missing recording info in session data.'
            log(msg, e)
            return dict(msg=msg, statusCode=400)

        # only commit if we had no exceptions until this point
        # and no error
        if not error:
            self.mysql.connection.commit()

            # extra, add the number of tokens (recordings) we have actually received from this speaker
            numRecs = self.getRecordingCount(speakerName, speakerId, deviceId)

            return dict(msg=json.dumps(dict(sessionId=sessionId, deviceId=deviceId, speakerId=speakerId, recsDelivered=numRecs)), 
                    statusCode=200)
        else:
            log('There was an error: {}, not committing to MySQL database.'.format(error))
            return dict(msg=error, statusCode=errorStatusCode)

    def writeRecToFilesystem(self, rec, token, sessionId, speakerName, lost=False):
        """
        Writes rec (as .wav) and token (as .txt with same name as rec) to filesystem at 
        app.config['MAIN_RECORDINGS_PATH']/session_<sessionId>/filename

        Parameters:
            rec         a werkzeug FileStorage object representing a .wav recording
            token       a string representing the prompt read during rec. None if 
                        there was no token, or an error in retrieving it.
            sessionId   id of session, None if error obtaining it.
            speakerName the name of the speaker, None if there was an error
            lost        True if there was an error in handling the metadata, means we 
                        still write the recording to file, just categorized as lost.

        Return:
            recName     returns the name of the saved recording (basename)
        """
        if not token:
            token = 'No prompt.'
        if not sessionId:
            sessionId = 'unknown'
        if not speakerName:
            speakerName = 'unknown'

        # save recordings to app.config['MAIN_RECORDINGS_PATH']/session_sessionId/filename
        sessionPath = os.path.join(self.recordings_path, 'session_{}'.format(sessionId))
        if lost:
            sessionPath = os.path.join(self.recordings_path, 'lost', 'session_{}'.format(sessionId))
        os.makedirs(sessionPath, exist_ok=True)
        
        recName = filename(speakerName) + '_' + filename(rec.filename)
        wavePath = os.path.join(sessionPath, recName)
        # rec is a werkzeug FileStorage object
        rec.save(wavePath)
        # save additional metadata to text file with same name as recording
        # open with utf8 to avoid encoding issues.
        # right now, only save the token
        with open(wavePath.replace('.wav','.txt'), mode='w', encoding='utf8') as f:
            f.write(token)

        return recName

    def getRecordingCount(self, speakerName, speakerId, deviceId):
        """
        Returns how many recordings this speaker has in our database.

        parameters:
            speakerName     name as it is in json we receive from frontend
            speakerId       id of speaker as in database
            deviceId        id of device as in database

        returns:
            recCnt          number of recordings from this speaker in database
                            -1 on failure

        Right now, takes the maximum of the recordings of speaker with supplied id
        and the total sum of all speakers with 'speakerName' and a common 'deviceId'.
        Meaning, if a speaker through a glitch has a couple versions of himself in the db
        (with same device tho) we count that.
        """
        try:
            cur = self.mysql.connection.cursor()

            #TODO combine into one query
            cur.execute('SELECT count(*) FROM recording '
                        'WHERE speakerId IN ( '
                            'SELECT id FROM speaker WHERE name=%s) '
                            'AND sessionId IN (SELECT id FROM session WHERE deviceId=%s)'
                        ,(speakerName, deviceId))

            cntByName = cur.fetchone()
            if cntByName is None:
                cntByName = 0
            else:
                cntByName = cntByName[0]
            cur.execute('SELECT count(*) FROM recording WHERE speakerId=%s', (speakerId,))
            cntById = cur.fetchone()
            if cntById is None:
                cntById = 0
            else:
                cntById = cntById[0]
        except MySQLError as e:
            msg = 'Error grabbing recording count for speaker with id={}'.format(speakerId)
            log(msg, e)
            return -1 # lets not fail, but return a sentinel value of -1

        return max(int(cntByName), int(cntById))


    def getTokens(self, numTokens):
        """
        Gets numTokens tokens randomly selected from the database and returns them in a nice json format.
        look at format in the client-server API
        
        Does not return any tokens marked with valid:FALSE in db.
        or it's: [{"id":id1, "token":token1}, {"id":id2, "token":token2}, ...]
        
        returns [] on failure
        """
        tokens = []
        try:
            cur = self.mysql.connection.cursor()
            # Get list of random tokens which are valid from the mysql database
            cur.execute('SELECT id, inputToken, valid FROM token WHERE valid=1 ORDER BY RAND() LIMIT %s',
                        (numTokens, ))
            tokens = cur.fetchall()
        except MySQLError as e:
            msg = 'Error getting tokens from database.'
            log(msg, e)
            return []

        jsonTokens = []
        # parse our tuple object from the cursor.execute into our desired json object
        for pair in tokens:
            jsonTokens.append({"id":pair[0], "token":pair[1]})
        return jsonTokens

    def getRecordingsInfo(self, sessionId, count=None) -> '[{"recId": ..., "token": str, "recPath": str - absolute path, "tokenId": ...}]':
        """Fetches info for the recordings of the session `sessionId`

        Parameters:
          sessionId    Only consider recordings from this session
          count        If set only return info for count newest recordings
                       otherwise fetch info for all recordings from session

        The returned list contains the newest recordings last, i.e. recordings are
        in ascending order with regard to recording id.
        """
        try:
            cur = self.mysql.connection.cursor()
            cur.execute('SELECT recording.id, recording.filename, token.inputToken, token.id FROM recording '
                        + 'JOIN token ON recording.tokenId=token.id '
                        + 'WHERE recording.sessionId=%s '
                        + 'ORDER BY recording.id ASC ', (sessionId,))

            if count is not None:
                rows = cur.fetchmany(size=count)
            else:
                rows = cur.fetchall()
        except MySQLError as e:
            msg = 'Error getting info for session recordings'
            log(msg, e)
            raise
        else:
            return json.dumps([dict(recId=recId, 
                                    recPath=os.path.join(self.recordings_path,'session_'+str(sessionId),recPath), 
                                    token=token, 
                                    tokenId=id)
                                for recId, recPath, token, id in rows])

    def sessionExists(self, sessionId) -> bool:
        """
        Checks to see if session with sessionId exists (is in database).
        """
        try:
            cur = self.mysql.connection.cursor()
            cur.execute('SELECT * FROM session WHERE id=%s', (sessionId,))
            if cur.fetchone():
                return True
        except MySQLError as e:
            msg = 'Error checking for session existence.'
            log(msg, e)
            raise
        else:
            return False

    def getFromSet(self, eval_set, progress, count):
        """
        Get link/prompt pairs from specified set in ascending order by recording id.

        Parameters:
            set         name of the set corresponding to evaluation_sets(eval_set) in database.
            progress    progress (index) into the set
            count       number of pairs to get

        A special set, Random, will receive count random recordings from the total recordings
        so far, unrelated to progress.

        Returns tuple
            (json, http_status_code)

        Returned JSON definition:
            [[recLinkN, promptN], .., [recLinkN+count, promptN+count]]

        where N is progress and recLink is the RECSURL + the relative path in the RECSROOT folder,
        e.g. '/recs/session_26/user_date.wav'. An error string on failure.
        """
        try:
            cur = self.mysql.connection.cursor()
            # select count random recordings from a special set (Random)
            if eval_set == 'Random':
                cur.execute('SELECT id FROM recording');
                recIds = [x[0] for x in cur.fetchall()]
                recIds = recIds[1:] # remove the placeholder recording introduced by populate_db.sql

                randIds = random.sample(recIds, count)
                randIds = tuple(randIds) # change to tuple because SQL syntax is 'WHERE id IN (1,2,3,..)'
                cur.execute('SELECT recording.sessionId, recording.filename, inputToken '+
                            'FROM recording, token '+
                            'WHERE recording.tokenId = token.id '+
                            'AND recording.id IN %s ',
                            (randIds,))
            else:
                # branch for the normal usage, taking from a specific set
                cur.execute('SELECT recording.sessionId, recording.filename, inputToken '+
                            'FROM recording, token, evaluation_sets '+
                            'WHERE recording.tokenId = token.id '+
                            'AND recording.id = evaluation_sets.recordingId '+
                            'AND eval_set=%s '+
                            'ORDER BY evaluation_sets.id ASC', (eval_set,))
            partialSet = [['{}/session_{}/{}'.format(RECSURL, sesId, filename), prompt]
                           for sesId, filename, prompt in cur.fetchall()]
        except MySQLError as e:
            msg = 'Error grabbing from set.'
            log(msg, e)
            return (msg, 500)

        if partialSet and eval_set != 'Random':
            return (partialSet[progress:progress+count], 200)
        elif partialSet and eval_set == 'Random':
            return (partialSet, 200) # not actually a partial set in this case, just set with count elements
        else:
            msg = 'No set by that name in database.'
            log(msg+' Set: {}'.format(eval_set))
            return (msg, 404)

    def processEvaluation(self, eval_set, data):
        """
        Process and save evaluation in database table: evaluation.

        Parameters:
            eval_set    name of the set corresponding to evaluation_sets table
            data        json on format:
                        [
                            {
                                "evaluator": "daphne",
                                "sessionId": 5,
                                "recordingFilename": "asdf_2016-03-05T11:11:09.287Z.wav",
                                "grade": 2,
                                "comments": "Bad pronunciation",
                                "skipped": false
                            },
                            ..
                        ]

        Returns (msg, http_status_code)
        """
        eval_set = str(eval_set)
        try:
            jsonDecoded = json.loads(data)
            #log('json: ', jsonDecoded)
        except (TypeError, ValueError) as e:
            msg = 'Evaluation data not on correct format.'
            log(msg, e)
            return (msg, 400)

        error = '' 
        errorStatusCode = 500
        for evaluation in jsonDecoded:
            evaluator, sessionId, recordingFilename, grade, comments, skipped = \
                None, None, None, None, None, None
            try:
                evaluator = evaluation['evaluator']
                sessionId = evaluation['sessionId']
                recordingFilename = evaluation['recordingFilename']
                grade = evaluation['grade']
                comments = evaluation['comments']
                skipped = evaluation['skipped']
            except KeyError as e:
                error = 'Some evaluation data not on correct format, wrong key.'
                errorStatusCode = 400
                log(error + ' Data: {}, eval_set: {}'.format(evaluation, eval_set), e)
                continue

            try:
                cur = self.mysql.connection.cursor()
                if eval_set == 'Random':
                    cur.execute('SELECT recording.id FROM recording '+
                                'WHERE recording.sessionId = %s '+
                                'AND recording.filename = %s ',
                                (sessionId, recordingFilename))
                else:
                    cur.execute('SELECT recording.id FROM recording, evaluation_sets '+
                                'WHERE evaluation_sets.recordingId = recording.id '+
                                'AND recording.sessionId = %s '+
                                'AND recording.filename = %s '+
                                'AND eval_set = %s',
                                (sessionId, recordingFilename, eval_set))
                try:
                    recId = cur.fetchone()[0]
                except TypeError as e:
                    error = 'Could not find a recording with some data.'
                    errorStatusCode = 400
                    log(error + ' Data: {}, eval_set: {}'.format(evaluation, eval_set), e)
                    continue

                cur.execute('INSERT INTO evaluation (recordingId, eval_set, evaluator, grade, comments, skipped) \
                             VALUES (%s, %s, %s, %s, %s, %s)', 
                            (recId, eval_set, evaluator, grade, comments, skipped))
            except MySQLError as e:
                error = 'Error inserting some evaluation into database.'
                errorStatusCode = 500
                log(error + ' Data: {}, eval_set: {}'.format(evaluation, eval_set), e)
                continue

        self.mysql.connection.commit()

        if error:
            return (error, errorStatusCode)
        else:
            return ('Successfully processed evaluation.', 200)

    def getSetInfo(self, eval_set):
        """
        Currently, only returns the number of elements in <eval_set> on format:
            {
                "count": 52
            }
        """
        if eval_set == 'Random':
            return (json.dumps(dict(count='???')), 200)

        try:
            cur = self.mysql.connection.cursor()
            cur.execute('SELECT COUNT(*) FROM evaluation_sets '+
                        'WHERE eval_set=%s ', (eval_set,))
            try:
                count = cur.fetchone()[0]
            except TypeError as e:
                msg = 'Could not find supplied set.'
                log(msg + ' Eval_set: {}'.format(eval_set), e)
                return (msg, 404)
        except MySQLError as e:
            msg = 'Error getting set info.'
            log(msg + ' Eval_set: {}'.format(eval_set), e)
            return (msg, 500)

        return (json.dumps(dict(count=count)), 200)

    def getUserProgress(self, user, eval_set):
        """
        Returns user progress into eval_set, format:
            {
                "progress": 541
            }
        """
        try:
            cur = self.mysql.connection.cursor()
            cur.execute('SELECT COUNT(*) FROM evaluation '+
                        'WHERE eval_set=%s '+
                        'AND evaluator=%s', (eval_set, user))
            # COUNT(*) always returns a number, so no need for a try block here
            progress = cur.fetchone()[0]
        except MySQLError as e:
            msg = 'Error getting user progress.'
            log(msg + ' Eval_set: {}, user: {}'.format(eval_set, user), e)
            return (msg, 500)

        return (json.dumps(dict(progress=progress)), 200)

    def getPossibleSets(self):
        """
        Returns possible sets, format:
            [
                "set1",
                "set2",
                ..
            ]
        or as in client-server API.
        """
        try:
            cur = self.mysql.connection.cursor()
            cur.execute('SELECT eval_set FROM evaluation_sets '+
                        'GROUP BY eval_set')
            sets = [x[0] for x in cur.fetchall()]
        except MySQLError as e:
            msg = 'Error getting possible sets.'
            log(msg, e)
            return (msg, 500)

        return (json.dumps(sets), 200)
