import struct
from pprint import pprint
from cassandra.cqlengine.connection import setup
from cassandra.cqlengine.named import NamedTable
from cassandra.cqlengine.models import Model
from cassandra.cqlengine import columns

from cassandra.cluster import Cluster


class ErrorTrackerTable(Model):
    __table_name__ = "Indexes"
    __keyspace__ = "crashdb"
    __table_name_case_sensitive__ = True
    key = columns.Blob(db_field="key", primary_key=True)
    column1 = columns.Text(db_field="column1", primary_key=True)
    value = columns.Blob(db_field="value")


setup(["127.0.0.1"], "crashdb")

# cluster = Cluster(["127.0.0.1"])
# session = cluster.connect("crashdb")

# print(session.execute('SELECT * FROM "Stacktrace"').one())


# print(ErrorTrackerTable.objects().count())
# print(ErrorTrackerTable.objects()[:10])

from daisy.cassandra_schema import OOPS, Indexes
from daisy import cassandra_schema

# oops_id = b"4000"
# OOPS.objects.create(key=oops_id, column1="Package", value="guy")
# OOPS.objects.create(key=oops_id, column1="RetraceStatus", value="Failure")
# OOPS.objects.create(key=oops_id, column1="RetraceFailureReason", value="Boink")
# pprint(OOPS.get_as_dict(key=b"4000"))
# unneeded_columns = ["RetraceStatus", "RetraceFailureReason"]
# # OOPS.objects.filter(key=oops_id, column1__in=unneeded_columns).delete()
# pprint(OOPS.get_as_dict(key=b"4000"))
# pprint(OOPS.objects.get(key=oops_id, column1="RetraceStatus")["value"])

mean_key = "20241210:Ubuntu 25.04:amd64"
count_key = "20241210:Ubuntu 25.04:amd64:count"
mean_key = "20120507:Ubuntu 12.04"
count_key = "20120507:Ubuntu 12.04:count"

# Indexes.objects.create(
#     key=b"mean_retracing_time",
#     column1=mean_key,
#     value=bytes.fromhex("4157247C"),
# )
# Indexes.objects.create(
#     key=b"mean_retracing_time",
#     column1=count_key,
#     value=bytes.fromhex("01"),
# )

idx = Indexes.get_as_dict(
    key=b"mean_retracing_time",
    column1__in=[
        mean_key,
        count_key,
    ],
)
pprint(idx)
idx[mean_key] = struct.unpack("!f", idx[mean_key])[0]
idx[count_key] = int.from_bytes(idx[count_key])
pprint(idx)

# stacktraces = NamedTable("crashdb", '"Stacktrace"')
# print(stacktraces.objects())
# obj = stacktraces.objects()[0]
# print(obj)
# # Stacktrace.objects().create(key=obj["key"], column1="Skia", value="Hello there")
# print(list(stacktraces.objects.allow_filtering().get(column1="Skiaa")))
# # stacktraces.objects().create(key=obj["key"], column1="Skia", value="Hello there")
