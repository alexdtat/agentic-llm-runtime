import csv
import json
import statistics
import sys

with open(sys.argv[1],newline='') as stream:
    values=[float(row['value']) for row in csv.DictReader(stream)]
print(json.dumps({'count':len(values),'mean':statistics.mean(values),'median':statistics.median(values)}))
