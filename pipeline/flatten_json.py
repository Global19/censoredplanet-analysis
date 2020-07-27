"""Beam pipeline for converting json scan files into bigquery tables."""

from __future__ import absolute_import

import argparse
import json
import logging
import datetime
import re
from pprint import pprint

import apache_beam as beam
from apache_beam.io import ReadFromText
from apache_beam.io import WriteToText
from apache_beam.io.gcp.internal.clients import bigquery
from apache_beam.io.gcp.gcsfilesystem import GCSFileSystem
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions

bigquery_schema = {
    'domain': 'string',
    'ip': 'string',
    'date': 'date',
    'start_time': 'timestamp',
    'end_time': 'timestamp',
    'retries': 'integer',
    'sent': 'string',
    'received': 'string',
    'error': 'string',
    'blocked': 'boolean',
    'success': 'boolean',
    'fail_sanity': 'boolean',
    'stateful_block': 'boolean'
}
# Future fields
"""
    'row_number', 'integer',
    'domain_category': 'string',
    'netblock': 'string',
    'asn': 'string',
    'as_name': 'string',
    'as_full_name': 'string',
    'as_traffic': 'integer',
    'as_class': 'string',
    'country': 'string',

"""


def get_bigquery_schema():
  """Return a beam bigquery schema for the output table."""
  table_schema = bigquery.TableSchema()

  for (name, field_type) in bigquery_schema.items():

    field_schema = bigquery.TableFieldSchema()
    field_schema.name = name
    field_schema.type = field_type
    field_schema.mode = 'nullable'  # all fields are flat
    table_schema.fields.append(field_schema)

  return table_schema


def read_scan_text(p, gcs, scan_type, start_date=None, end_date=None):
  """Read in the given json files for a date segment.

  Args:
    p: beam pipeline object
    gcs: GCSFileSystem object
    scan_type: one of "echo", "discard", "http", "https"
    start_date: date object, only files at or after this date will be read
    end_date: date object, only files before or after this date will be read

  Returns:
    A PCollection<Pair<filename, line>>
    of all the lines in the files keyed by filename.

    (currently this is stil just a PCollection of lines)
  """
  #gcs_reader = GCSFileReader(gcs)

  filename_regex = 'gs://firehook-scans/' + scan_type + '/**/results.json'
  filenames = (
      p
      | 'enumerate files' >> beam.Create(
          [m.metadata_list for m in gcs.match([filename_regex])])
      | 'metadata_list to filepath' >> beam.FlatMap(
          lambda metadata_list: [metadata.path for metadata in metadata_list]))

  filtered_names = (
      filenames
      | 'filter file dates' >> beam.Filter(between_dates, start_date, end_date))

  lines = (
      filtered_names
      | 'read lines' >> beam.FlatMap(lambda filename: gcs.open(filename)))

  return lines


def between_dates(filename, start_date, end_date):
  """Return true if a filename is between (or matches either of) two dates.

  Args:
    filename: string of the format
    "gs://firehook-scans/http/CP_Quack-http-2020-05-11-01-02-08/results.json"
    start_date: date object
    end_date: date object

  Returns:
    boolean
  """
  date = datetime.date.fromisoformat(
      re.findall('\d\d\d\d-\d\d-\d\d', filename)[0])
  return start_date <= date <= end_date


def flatten_measurement(line):
  """Flatten a measurement string into several roundtrip rows.

  Args:
    line: a json string describing a censored planet measurement. example
    {'Keyword': 'test.com,
     'Server': '1.2.3.4',
     'Results': [{'Success': true},
                 {'Success': false}]}

  Returns:
    an array of dicts containing individual roundtrip information
    [{'column_name': field_value}]
    example
    [{'domain': 'test.com', 'ip': '1.2.3.4', 'success': true}
     {'domain': 'test.com', 'ip': '1.2.3.4', 'success': true}]
  """
  rows = []

  try:
    scan = json.loads(line)
  except json.decoder.JSONDecodeError as e:
    logging.warn('JSONDecodeError: %s', e)
    return rows

  for result in scan['Results']:
    rows.append({
        'domain': scan['Keyword'],
        'ip': scan['Server'],
        'date': result['StartTime'][:10],
        'start_time': result['StartTime'],
        'end_time': result['EndTime'],
        'retries': scan['Retries'],
        'sent': result['Sent'],
        'received': result.get('Received', ''),
        'error': result.get('Error', ''),
        'blocked': scan['Blocked'],
        'success': result['Success'],
        'fail_sanity': scan['FailSanity'],
        'stateful_block': scan['StatefulBlock'],
    })
  return rows


def run(argv=None, save_main_session=True):
  pipeline_args = [
      # DataflowRunner or DirectRunner
      '--runner=DataflowRunner',
      '--project=firehook-censoredplanet',
      '--region=us-east1',
      '--staging_location=gs://firehook-dataflow-test/staging',
      '--temp_location=gs://firehook-dataflow-test/temp',
      '--job_name=flatten-json-https-job',
  ]
  pipeline_options = PipelineOptions(pipeline_args)
  pipeline_options.view_as(SetupOptions).save_main_session = save_main_session
  gcs = GCSFileSystem(PipelineOptions(pipeline_args))

  with beam.Pipeline(options=pipeline_options) as p:
    scan_type = 'http'

    start_date = datetime.date.fromisoformat('2020-05-07')
    end_date = datetime.date.fromisoformat('2020-05-11')

    lines = read_scan_text(
        p, gcs, scan_type, start_date=start_date, end_date=end_date)

    rows = (lines | 'flatten json' >> (beam.FlatMap(flatten_measurement)))

    bigquery_table = 'firehook-censoredplanet:' + scan_type + '_results.scan'

    rows | 'Write' >> beam.io.WriteToBigQuery(
        bigquery_table,
        schema=get_bigquery_schema(),
        create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
        # WRITE_TRUNCATE is slow when testing.
        write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND)
    #write_disposition=beam.io.BigQueryDisposition.WRITE_TRUNCATE)


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  run()