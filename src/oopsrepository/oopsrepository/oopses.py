# oops-repository is Copyright 2011 Canonical Ltd.
#
# Canonical Ltd ("Canonical") distributes the oops-repository source code under
# the GNU Affero General Public License, version 3 ("AGPLv3"). See the file
# LICENSE in the source tree for more information.

"""basic operations on oopses in the db."""

import json
import re
import time
import uuid
import datetime
from hashlib import sha1, md5
import pycassa

from cassandra import ConsistencyLevel
from cassandra.cluster import Cluster
from cassandra.auth import PlainTextAuthProvider
from cassandra.protocol import InvalidRequestException
from cassandra.query import SimpleStatement

from binascii import hexlify

DAY = 60*60*24
MONTH = DAY*30

_pycassa_connection_pool = None
def pycassa_connection_pool(config):
    '''Caching constructor of pycassa.ConnectionPool'''
    global _pycassa_connection_pool
    if _pycassa_connection_pool:
        return _pycassa_connection_pool
    pool_size = config.get('pool_size', '')
    max_overflow = config.get('max_overflow', '')
    # I have no idea why max_retries is evaluating as 0 when not set, but here
    # we are, brute forcing this.
    creds = {'username': config['username'], 'password': config['password']}
    _pycassa_connection_pool = pycassa.ConnectionPool(config['keyspace'],
                                                      config['host'],
                                                      pool_size=pool_size,
                                                      max_overflow=max_overflow,
                                                      max_retries=5,
                                                      timeout=30,
                                                      credentials=creds)
    return _pycassa_connection_pool

_cassandra_session = None
def cassandra_session(config):
    '''Caching constructor of cassandra connection'''
    global _cassandra_session
    if _cassandra_session:
        return _cassandra_session
    auth_provider = PlainTextAuthProvider(
        username=config['username'], password=config['password'])
    cluster = Cluster(config['host'], auth_provider=auth_provider)
    _cassandra_session = cluster.connect(config['keyspace'])
    _cassandra_session.default_consistency_level = ConsistencyLevel.LOCAL_ONE
    return _cassandra_session

def prune(config):
    """Remove OOPSES that are over 30 days old."""
    pool = pycassa_connection_pool(config)
    dayoops_cf = pycassa.ColumnFamily(pool, 'DayOOPS')
    oops_cf = pycassa.ColumnFamily(pool, 'OOPS')
    # Find days to prune
    days = set()
    prune_to = time.strftime('%Y%m%d', time.gmtime(time.time() - MONTH))
    for key, _ in dayoops_cf.get_range():
        if key < prune_to:
            days.add(key)
    if not days:
        return
    # collect all the oopses (buffers all in memory; may want to make
    # incremental in future)
    batch_size = 10000
    oopses = []
    for day in days:
        columns_found = batch_size
        start_col = ''
        while columns_found==batch_size:
            columns = dayoops_cf.get(day, column_start=start_col)
            columns_found = len(columns)
            for column, oopsid in columns.items():
                start_col = column
                oopses.append(oopsid)
    # Remove the oopses
    batch = oops_cf.batch()
    map(batch.remove, oopses)
    batch.send()
    # Clean out the days aggregates
    # Clean out the days index
    batch = dayoops_cf.batch()
    map(batch.remove, days)
    batch.send()

def insert(config, oopsid, oops_json, user_token=None, fields=None,
           proposed_pkg=False):
    """Insert an OOPS into the system.

    :return: The day which the oops was filed under.
    """
    # make sure the oops report is a json dict, and break out each key to a
    # separate column. For now, rather than worrying about typed column values
    # we just coerce them all to strings.
    oops_dict = json.loads(oops_json)
    assert isinstance(oops_dict, dict)
    insert_dict = {}
    for key, value in oops_dict.items():
        insert_dict[key] = json.dumps(value)
    return _insert(config, oopsid, insert_dict, user_token, fields,
                   proposed_pkg)

def insert_dict(session, oopsid, oops_dict, user_token=None, fields=None,
                proposed_pkg=False, ttl=False):
    """Insert an OOPS into the system.

    :return: The day which the oops was filed under.
    """
    assert isinstance(oops_dict, dict)
    return _insert(session, oopsid, oops_dict, user_token, fields,
                   proposed_pkg, ttl)

def _insert(session, oopsid, insert_dict, user_token=None, fields=None,
            proposed_pkg=False, ttl=False):
    """Internal function. Do not call this directly.

    :param oopsid: The identifier for this OOPS.
    :param insert_dict: A dictionary containing the data to associate this OOPS
        with.
    :param user_token: An identifier for the user who experienced this OOPS.
    :param fields: A dictionary containing keys to increment counters for.
    :param proposed_pkg: True if the report's Tags contain package-from-proposed
    :param ttl: boolean for setting the time to live for the column
    :return: The day which the oops was filed under.
    """
    if isinstance(session, dict):
        session = cassandra_session(session)
    day_key = time.strftime('%Y%m%d', time.gmtime())
    hex_day_key = '0x' + hexlify(day_key)
    now_uuid = uuid.uuid1()

    hex_oopsid = '0x' + hexlify(oopsid)
    for key, value in insert_dict.iteritems():
        # try to avoid an OOPS re column1 being missing
        if not key:
            continue
        cql_key = key.replace("'", "''")
        cql_value = value.replace("'", "''")
        cql_query = ("INSERT INTO \"%s\" (key, column1, value) VALUES (%s, '%s', '%s')"
                     % ('OOPS', hex_oopsid, cql_key, cql_value))
        # delete the column after 30 days
        if ttl:
            cql_query += ' USING TTL 2592000'
        try:
            session.execute(SimpleStatement(cql_query))
        except InvalidRequestException:
            continue
    automated_testing = False
    if user_token.startswith('deadbeef'):
        automated_testing = True

    session.execute(SimpleStatement
        ("INSERT INTO \"%s\" (key, column1, value) VALUES (%s, %s, %s)"
        % ('DayOOPS', hex_day_key, now_uuid, hex_oopsid)))
    # Systems running automated tests should not be included in the OOPS count.
    if not automated_testing:
        # Provide quick lookups of the total number of oopses for the day by
        # maintaining a counter.
        hex_oopses = '0x' + hexlify('oopses')
        session.execute(SimpleStatement
            ("UPDATE \"%s\" SET value = value + 1 WHERE key = %s AND column1 ='%s'"
            % ('Counters', hex_oopses, day_key)))
        if fields:
            for field in fields:
                field = field.encode('ascii', errors='replace')
                cql_field = field.replace("'", "''")
                hex_oopses_field = '0x' + hexlify('oopses:%s' % cql_field)
                session.execute(SimpleStatement
                    ("UPDATE \"%s\" SET value = value + 1 WHERE key = %s AND column1 ='%s'"
                    % ('Counters', hex_oopses_field, day_key)))
        if proposed_pkg:
            for field in fields:
                field = field.encode('ascii', errors='replace')
                cql_field = field.replace("'", "''")
                hex_oopses_field = '0x' + hexlify('oopses:%s' % cql_field)
                session.execute(SimpleStatement
                    ("UPDATE \"%s\" SET value = value + 1 WHERE key = %s AND column1 ='%s'"
                    % ('CountersForProposed', hex_oopses_field, day_key)))

    if user_token:
        hex_user_token = '0x' + hexlify(user_token)
        session.execute(SimpleStatement
            ("INSERT INTO \"%s\" (key, column1, value) VALUES (%s, '%s', %s)"
             % ('UserOOPS', hex_user_token, oopsid, '0x')))
        # Build a unique identifier for crash reports to prevent the same
        # crash from being reported multiple times.
        date = insert_dict.get('Date', '')
        exec_path = insert_dict.get('ExecutablePath', '')
        proc_status = insert_dict.get('ProcStatus', '')
        if date and exec_path and proc_status:
            crash_id = '%s:%s:%s' % (date, exec_path, proc_status)
            if type(crash_id) == unicode:
                crash_id = crash_id.encode('utf-8')
            crash_id = md5(crash_id).hexdigest()
            session.execute(SimpleStatement
                ("INSERT INTO \"%s\" (key, column1, value) VALUES (%s, '%s', %s)"
                % ('SystemOOPSHashes', hex_user_token, crash_id, '0x')))
        # TODO we can drop this once we're successfully using ErrorsByRelease.
        # We'll have to first ensure that all the calculated historical data is
        # in UniqueUsers90Days.
        session.execute(SimpleStatement
            ("INSERT INTO \"%s\" (key, column1, value) VALUES (%s, '%s', %s)"
            % ('DayUsers', hex_day_key, user_token, '0x')))
        if fields:
            for field in fields:
                field = field.encode('ascii', errors='replace')
                cql_field = field.replace("'", "''")
                hex_field_day = '0x' + hexlify('%s:%s' % (field, day_key))
                session.execute(SimpleStatement
                    ("INSERT INTO \"%s\" (key, column1, value) VALUES (%s, '%s', %s)"
                    % ('DayUsers', hex_field_day, user_token, '0x')))

    return day_key

def bucket(session, oopsid, bucketid, fields=None, proposed_fields=False):
    """Adds an OOPS to a bucket, a collection of OOPSes that form a single
    issue. If the bucket does not exist, it will be created.

    :return: The day which the bucket was filed under.
    """
    # retracer.py hasn't been updated to pass in a python-cassandra session
    if isinstance(session, dict):
        session = cassandra_session(session)
    cql_bucketid = bucketid.replace("'", "''")
    # Get the timestamp.
    try:
        hex_oopsid = '0x' + hexlify(oopsid)
        results = session.execute(SimpleStatement
            ("SELECT WRITETIME (value) FROM \"%s\" WHERE key = %s"
             % ('OOPS', hex_oopsid)))
        timestamp = [r[0] for r in results][0]
        day_key = time.strftime('%Y%m%d', time.gmtime(timestamp / 1000000))
    except IndexError:
        # Eventual consistency. This OOPS probably occurred today.
        day_key = time.strftime('%Y%m%d', time.gmtime())

    session.execute(SimpleStatement
        ("INSERT INTO \"%s\" (key, column1, value) VALUES ('%s', %s, %s)"
        % ('Bucket', cql_bucketid, uuid.UUID(oopsid), '0x')))
    session.execute(SimpleStatement
        ("INSERT INTO \"%s\" (key, key2, column1, value) VALUES ('%s', '%s', '%s', %s)"
        % ('DayBuckets', day_key, cql_bucketid, oopsid, '0x')))

    if fields is not None:
        resolutions = (day_key[:4], day_key[:6], day_key)
        # All buckets for the given resolution.
        dbc_update = session.prepare('UPDATE "DayBucketsCount" SET value = value + 1 WHERE key = ? and column1 = ?')
        for field in fields:
            for resolution in resolutions:
                # We have no way of knowing whether an increment has been
                # performed if the write fails unexpectedly (CASSANDRA-2495).
                # We will apply eventual consistency to this problem and
                # tolerate slightly inaccurate counts for the span of a single
                # day, cleaning up once this period has passed. This will be
                # done by counting the number of columns in DayBuckets for the
                # day and bucket ID.
                field_resolution = ':'.join((field, resolution))
                session.execute(dbc_update,
                                [field_resolution, cql_bucketid])
        for resolution in resolutions:
            session.execute(dbc_update,
                            [resolution, cql_bucketid])
    return day_key

def update_bucket_versions(session, bucketid, version,
                           release=None, oopsid=None):
    # retracer.py hasn't been updated to pass in a python-cassandra session
    if isinstance(session, dict):
        session = cassandra_session(session)
    if release:
        # Use the current day, rather than the day of the OOPS because this is
        # specifically used for cleaning up counters nightly. If a very old
        # OOPS gets processed here, we should still clean it up when we're
        # handling the data for today.
        day_key = time.strftime('%Y%m%d', time.gmtime())
        hex_day_key = '0x' + hexlify(day_key)
        cql_bucketid = bucketid.replace("'", "''")

        uuid_oopsid = uuid.UUID(oopsid)
        release = release.encode('ascii', errors='ignore')
        cql_release = release.replace("'", "''")
        if version:
            version = version.encode('ascii', errors='replace')

        # When correcting the counts in bv_count, we'll iterate
        # BucketVersionsDay for day_key. For each of these columns, we'll look
        # up the correct value by calling bv_full.get_count(...).
        session.execute(SimpleStatement
            ("INSERT INTO \"%s\" (key, key2, key3, column1, value) VALUES ('%s', '%s', '%s', %s, %s)"
            % ('BucketVersionsFull', cql_bucketid, cql_release, version, uuid_oopsid, '0x')))
        session.execute(SimpleStatement
            ("INSERT INTO \"%s\" (key, column1, column2, column3, value) VALUES (%s, '%s', '%s', '%s', %s)"
            % ('BucketVersionsDay', hex_day_key, cql_bucketid, cql_release, version, '0x')))

        session.execute(SimpleStatement
            ("UPDATE \"%s\" SET value = value + 1 WHERE key = '%s' AND column1 ='%s' AND column2 = '%s'"
            % ('BucketVersionsCount', cql_bucketid, cql_release, version)))
    else:
        session.execute(SimpleStatement
            ("UPDATE \"%s\" SET value = value + 1 WHERE key = '%s' AND column1 ='%s'"
            % ('BucketVersions', cql_bucketid, version)))

def query_bucket_versions(config, bucketid):
    pool = pycassa_connection_pool(config)
    bucketversions_cf = pycassa.ColumnFamily(pool, 'BucketVersions')
    bucketversions = bucketversions_cf.xget(bucketid)
    if not bucketversions:
        return None
    return bucketversions

def update_errors_by_release(session, oops_id, system_token, release):
    # retracer.py hasn't been updated to pass in a python-cassandra session
    if isinstance(session, dict):
        session = cassandra_session(session)
    release = release.encode('utf8')
    cql_release = release.replace("'", "''")
    today = datetime.datetime.today()
    today = today.replace(hour=0, minute=0, second=0, microsecond=0)

    results = session.execute(SimpleStatement
        ("SELECT value FROM \"%s\" WHERE key = '%s' and column1 = '%s'"
        % ('FirstError', cql_release, system_token)))
    try:
        first_error_date = [row[0] for row in results][0]
    except IndexError:
        session.execute(SimpleStatement
            ("INSERT INTO \"%s\" (key, column1, value) VALUES ('%s', '%s', '%s')"
            % ('FirstError', cql_release, system_token, today)))
        first_error_date = today

    # We use the OOPS ID rather than the system identifier here because we want
    # each crash from a system to take up a new column in this column family.
    # Each one of those columns should be associated with the date of the first
    # error for the system in this release.
    # 
    # Remember, we're ultimately tracking errors here, not systems, but we need
    # the system identifier to know the first occurrence of an error in the
    # release for that machine.
    #
    # For the given release for today, the crash should be weighted by the
    # first time an error occurred in the release for the system this came
    # from.  Multiplied by their weight and summed together, these form the
    # numerator of our average errors per calendar day calculation.

    session.execute(SimpleStatement
        ("INSERT INTO \"%s\" (key, key2, column1, value) VALUES ('%s', '%s', %s, '%s')"
        % ('ErrorsByRelease', cql_release, today, oops_id, first_error_date)))
    session.execute(SimpleStatement
        ("INSERT INTO \"%s\" (key, key2, column1, value) VALUES ('%s', '%s', '%s', %s)"
        % ('SystemsForErrorsByRelease', cql_release, today, system_token, '0x')))

def update_bucket_metadata(session, bucketid, source, version, comparator, release=''):
    # retracer.py hasn't been updated to pass in a python-cassandra session
    if isinstance(session, dict):
        session = cassandra_session(session)
    # We only update the first and last seen version fields. We do not update
    # the current version field as talking to Launchpad is an expensive
    # operation, and we can do that out of band.
    metadata = {}
    bucketmetadata = {}
    release_re = re.compile('^Ubuntu \d\d.\d\d$')
    hex_bucketid = '0x' + hexlify(bucketid)
    cql_bucketid = bucketid.replace("'", "''")

    bucketmetadata_rows = session.execute(SimpleStatement
        ("SELECT column1, value FROM \"%s\" WHERE key = %s"
         % ('BucketMetadata', hex_bucketid)))
    for row in bucketmetadata_rows:
        bucketmetadata[row[0]] = row[1]
    try:
        # TODO: Drop the FirstSeen and LastSeen fields once BucketVersionsCount
        # is deployed, since we can just do a get(column_count=1) for the first
        # seen version and get(column_reversed=True, column_count=1) for the
        # last seen version.
        # N.B.: This presumes that we are using the DpkgComparator which we
        # won't be when we move to DSE.
        lastseen = bucketmetadata['LastSeen']
        if (not lastseen or comparator(lastseen, version) < 0):
            metadata['LastSeen'] = version
        lastseen_release = bucketmetadata['LastSeenRelease']
        # Some funny releases were already written to LastSeenRelease,
        # see LP: #1805912, ensure they are overwritten.
        if lastseen_release and not release_re.match(lastseen_release):
            lastseen_release = None
        if (not lastseen_release or \
                (lastseen_release.split()[-1] < release.split()[-1])):
            metadata['LastSeenRelease'] = release
        firstseen = bucketmetadata['FirstSeen']
        if (not firstseen or comparator(firstseen, version) > 0):
            metadata['FirstSeen'] = version
        firstseen_release = bucketmetadata['FirstSeenRelease']
        # Some funny releases were already written to FirstSeenRelease,
        # see LP: #1805912, ensure they are overwritten.
        if firstseen_release and not release_re.match(firstseen_release):
            firstseen_release = None
        if (not firstseen_release or \
                (release.split()[-1] < firstseen_release.split()[-1])):
            metadata['FirstSeenRelease'] = release
    except KeyError:
        metadata['FirstSeen'] = version
        metadata['LastSeen'] = version
        if release:
            metadata['FirstSeenRelease'] = release
            metadata['LastSeenRelease'] = release

    if release:
        k = '~%s:FirstSeen' % release
        firstseen = metadata.get(k, None)
        if (not firstseen or comparator(firstseen, version) > 0):
            metadata[k] = version
        k = '~%s:LastSeen' % release
        lastseen = metadata.get(k, None)
        if (not lastseen or comparator(lastseen, version) < 0):
            metadata[k] = version

    if metadata:
        metadata['Source'] = source
        bmd_insert = session.prepare('INSERT INTO "BucketMetadata" \
            (key, column1, value) VALUES (?, ?, ?)')
        for k in metadata:
            # a prepared statement seems to convert into hex so using hexlify
            # with bucketid is not needed
            session.execute(bmd_insert, [cql_bucketid, k, metadata[k]])

def update_bucket_systems(session, bucketid, system, version=None):
    '''Keep track of the unique systems in a bucket with a specific version of
       software.'''
    # retracer.py hasn't been updated to pass in a python-cassandra session
    if isinstance(session, dict):
        session = cassandra_session(session)
    if not system or not version:
        return
    version = version.encode('ascii', errors='replace')
    if not version:
        # If all we had were unicode characters.
        return
    cql_bucketid = bucketid.replace("'", "''")
    session.execute(SimpleStatement
        ("INSERT INTO \"%s\" (key, key2, column1, value) VALUES ('%s', '%s', '%s', %s)"
        % ('BucketVersionSystems2', cql_bucketid, version, system, '0x')))

def update_source_version_buckets(session, source, version, bucketid):
    # retracer.py hasn't been updated to pass in a python-cassandra session
    if isinstance(session, dict):
        session = cassandra_session(session)
    cql_bucketid = bucketid.replace("'", "''")
    # according to debian policy neither the package or version should have
    # utf8 in it but either some archives do not know that or something is
    # wonky with apport
    source = source.encode('ascii', errors='replace')
    version = version.encode('ascii', errors='replace')
    session.execute(SimpleStatement
        ("INSERT INTO \"%s\" (key, key2, column1, value) VALUES ('%s', '%s', '%s', %s)"
        % ('SourceVersionBuckets', source, version, cql_bucketid, '0x')))

def update_bucket_hashes(session, bucketid):
    '''Keep a mapping of SHA1 hashes to the buckets they represent.
       These hashes will be used for shorter bucket URLs.'''
    # retracer.py hasn't been updated to pass in a python-cassandra session
    if isinstance(session, dict):
        session = cassandra_session(session)
    cql_bucketid = bucketid.replace("'", "''")
    bucket_sha1 = sha1(bucketid).hexdigest()
    hex_bucket_sha1 = '0x' + hexlify(bucket_sha1)
    k = 'bucket_%s' % bucket_sha1[0]
    hex_k = '0x' + hexlify(k)
    session.execute(SimpleStatement
        ("INSERT INTO \"%s\" (key, column1, value) VALUES (%s, %s, '%s')"
        % ('Hashes', hex_k, hex_bucket_sha1, cql_bucketid)))
