#!/usr/bin/python2.7

import sys
import datetime

from cassandra import ConsistencyLevel
from cassandra.cluster import Cluster
from cassandra.auth import PlainTextAuthProvider
from cassandra.query import SimpleStatement

from daisy import config

auth_provider = PlainTextAuthProvider(
    username=config.cassandra_username, password=config.cassandra_password)
cluster = Cluster(config.cassandra_hosts, auth_provider=auth_provider)
session = cluster.connect(config.cassandra_keyspace)
session.default_consistency_level = ConsistencyLevel.LOCAL_ONE


# Utilities
def _date_range_iterator(start, finish):
    # Iterate all the values including and between the start and finish date
    # string.
    while start <= finish:
        yield start.strftime('%Y%m%d')
        start += datetime.timedelta(days=1)


# Main
if __name__ == '__main__':
    if '--dry-run' in sys.argv:
        dry_run = True
        sys.argv.remove('--dry-run')
    else:
        dry_run = False
    if len(sys.argv) > 2:
        d = datetime.datetime.strptime(sys.argv[2], '%Y%m%d')
        formatted = sys.argv[2]
    elif len(sys.argv) == 2:
        # Yesterday
        d = datetime.datetime.today() - datetime.timedelta(days=1)
        formatted = d.strftime('%Y%m%d')
    else:
        print >>sys.stderr, "Usage: release_name [date]"
        sys.exit(1)
    release = sys.argv[1]
    i = _date_range_iterator(d - datetime.timedelta(days=89), d)
    users = set()
    day_count = 0
    for date in i:
        if dry_run:
            print('looking up %s' % date)
            day_count += 1
        user_count = 0
        # don't need to use hexlify and bytearray because that's redundant
        hex_daterelease = bytearray('%s:%s' % (release, date))
        # column1 is the system uuid
        results = session.execute(SimpleStatement(
                    "SELECT column1 FROM \"DayUsers\" WHERE key=%s"),
                    [hex_daterelease])
        rows = [row for row in results]
        user_count += len(rows)
        # row[0] is column1 which is the system uuid
        users.update([row[0] for row in rows])
        if dry_run:
            print('%s' % user_count)
    # value is the number of users
    uu_results = session.execute(SimpleStatement(
        "SELECT value from \"UniqueUsers90Days\" WHERE key=%s and column1=%s"),
        [release, formatted])
    try:
        uu_count = [r[0] for r in uu_results][0]
    except IndexError:
        uu_count = 0
    print('Was %s' % uu_count)
    print('Now %s' % len(users))
    if not dry_run:
        session.execute(SimpleStatement
                        ("INSERT INTO \"%s\" (key, column1, value) \
                         VALUES ('%s', '%s', %d)"
                         % ('UniqueUsers90Days',
                            release, formatted, len(users))))
    else:
        print('%s:%s' % (release, len(users)))
    print('from %s days' % day_count)
