#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Unit tests for BigQuery sources and sinks."""
from __future__ import absolute_import

import datetime
import decimal
import json
import logging
import re
import time
import unittest

import hamcrest as hc
import mock
from future.utils import iteritems

import apache_beam as beam
from apache_beam.internal.gcp.json_value import to_json_value
from apache_beam.io.gcp.bigquery import JSON_COMPLIANCE_ERROR
from apache_beam.io.gcp.bigquery import RowAsDictJsonCoder
from apache_beam.io.gcp.bigquery import TableRowJsonCoder
from apache_beam.io.gcp.bigquery import parse_table_schema_from_json
from apache_beam.io.gcp.internal.clients import bigquery
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.transforms.display import DisplayData
from apache_beam.transforms.display_test import DisplayDataItemMatcher

# Protect against environments where bigquery library is not available.
# pylint: disable=wrong-import-order, wrong-import-position
try:
  from apitools.base.py.exceptions import HttpError
except ImportError:
  HttpError = None
# pylint: enable=wrong-import-order, wrong-import-position


@unittest.skipIf(HttpError is None, 'GCP dependencies are not installed')
class TestRowAsDictJsonCoder(unittest.TestCase):

  def test_row_as_dict(self):
    coder = RowAsDictJsonCoder()
    test_value = {'s': 'abc', 'i': 123, 'f': 123.456, 'b': True}
    self.assertEqual(test_value, coder.decode(coder.encode(test_value)))

  def test_decimal_in_row_as_dict(self):
    decimal_value = decimal.Decimal('123456789.987654321')
    coder = RowAsDictJsonCoder()
    # Bigquery IO uses decimals to represent NUMERIC types.
    # To export to BQ, it's necessary to convert to strings, due to the
    # lower precision of JSON numbers. This means that we can't recognize
    # a NUMERIC when we decode from JSON, thus we match the string here.
    test_value = {'f': 123.456,
                  'b': True,
                  'numerico': decimal_value}
    output_value = {'f': 123.456,
                    'b': True,
                    'numerico': str(decimal_value)}
    self.assertEqual(output_value, coder.decode(coder.encode(test_value)))

  def json_compliance_exception(self, value):
    with self.assertRaisesRegexp(ValueError, re.escape(JSON_COMPLIANCE_ERROR)):
      coder = RowAsDictJsonCoder()
      test_value = {'s': value}
      coder.decode(coder.encode(test_value))

  def test_invalid_json_nan(self):
    self.json_compliance_exception(float('nan'))

  def test_invalid_json_inf(self):
    self.json_compliance_exception(float('inf'))

  def test_invalid_json_neg_inf(self):
    self.json_compliance_exception(float('-inf'))


@unittest.skipIf(HttpError is None, 'GCP dependencies are not installed')
class TestTableRowJsonCoder(unittest.TestCase):

  def test_row_as_table_row(self):
    schema_definition = [
        ('s', 'STRING'),
        ('i', 'INTEGER'),
        ('f', 'FLOAT'),
        ('b', 'BOOLEAN'),
        ('n', 'NUMERIC'),
        ('r', 'RECORD'),
        ('g', 'GEOGRAPHY')]
    data_definition = [
        'abc',
        123,
        123.456,
        True,
        decimal.Decimal('987654321.987654321'),
        {'a': 'b'},
        'LINESTRING(1 2, 3 4, 5 6, 7 8)']
    str_def = ('{"s": "abc", '
               '"i": 123, '
               '"f": 123.456, '
               '"b": true, '
               '"n": "987654321.987654321", '
               '"r": {"a": "b"}, '
               '"g": "LINESTRING(1 2, 3 4, 5 6, 7 8)"}')
    schema = bigquery.TableSchema(
        fields=[bigquery.TableFieldSchema(name=k, type=v)
                for k, v in schema_definition])
    coder = TableRowJsonCoder(table_schema=schema)

    def value_or_decimal_to_json(val):
      if isinstance(val, decimal.Decimal):
        return to_json_value(str(val))
      else:
        return to_json_value(val)

    test_row = bigquery.TableRow(
        f=[bigquery.TableCell(v=value_or_decimal_to_json(e))
           for e in data_definition])

    self.assertEqual(str_def, coder.encode(test_row))
    self.assertEqual(test_row, coder.decode(coder.encode(test_row)))
    # A coder without schema can still decode.
    self.assertEqual(
        test_row, TableRowJsonCoder().decode(coder.encode(test_row)))

  def test_row_and_no_schema(self):
    coder = TableRowJsonCoder()
    test_row = bigquery.TableRow(
        f=[bigquery.TableCell(v=to_json_value(e))
           for e in ['abc', 123, 123.456, True]])
    with self.assertRaisesRegexp(AttributeError,
                                 r'^The TableRowJsonCoder requires'):
      coder.encode(test_row)

  def json_compliance_exception(self, value):
    with self.assertRaisesRegexp(ValueError, re.escape(JSON_COMPLIANCE_ERROR)):
      schema_definition = [('f', 'FLOAT')]
      schema = bigquery.TableSchema(
          fields=[bigquery.TableFieldSchema(name=k, type=v)
                  for k, v in schema_definition])
      coder = TableRowJsonCoder(table_schema=schema)
      test_row = bigquery.TableRow(
          f=[bigquery.TableCell(v=to_json_value(value))])
      coder.encode(test_row)

  def test_invalid_json_nan(self):
    self.json_compliance_exception(float('nan'))

  def test_invalid_json_inf(self):
    self.json_compliance_exception(float('inf'))

  def test_invalid_json_neg_inf(self):
    self.json_compliance_exception(float('-inf'))


@unittest.skipIf(HttpError is None, 'GCP dependencies are not installed')
class TestTableSchemaParser(unittest.TestCase):
  def test_parse_table_schema_from_json(self):
    string_field = bigquery.TableFieldSchema(
        name='s', type='STRING', mode='NULLABLE', description='s description')
    number_field = bigquery.TableFieldSchema(
        name='n', type='INTEGER', mode='REQUIRED', description='n description')
    record_field = bigquery.TableFieldSchema(
        name='r', type='RECORD', mode='REQUIRED', description='r description',
        fields=[string_field, number_field])
    expected_schema = bigquery.TableSchema(fields=[record_field])
    json_str = json.dumps({'fields': [
        {'name': 'r', 'type': 'RECORD', 'mode': 'REQUIRED',
         'description': 'r description', 'fields': [
             {'name': 's', 'type': 'STRING', 'mode': 'NULLABLE',
              'description': 's description'},
             {'name': 'n', 'type': 'INTEGER', 'mode': 'REQUIRED',
              'description': 'n description'}]}]})
    self.assertEqual(parse_table_schema_from_json(json_str),
                     expected_schema)


@unittest.skipIf(HttpError is None, 'GCP dependencies are not installed')
class TestBigQuerySource(unittest.TestCase):

  def test_display_data_item_on_validate_true(self):
    source = beam.io.BigQuerySource('dataset.table', validate=True)

    dd = DisplayData.create_from(source)
    expected_items = [
        DisplayDataItemMatcher('validation', True),
        DisplayDataItemMatcher('table', 'dataset.table')]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

  def test_table_reference_display_data(self):
    source = beam.io.BigQuerySource('dataset.table')
    dd = DisplayData.create_from(source)
    expected_items = [
        DisplayDataItemMatcher('validation', False),
        DisplayDataItemMatcher('table', 'dataset.table')]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

    source = beam.io.BigQuerySource('project:dataset.table')
    dd = DisplayData.create_from(source)
    expected_items = [
        DisplayDataItemMatcher('validation', False),
        DisplayDataItemMatcher('table', 'project:dataset.table')]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

    source = beam.io.BigQuerySource('xyz.com:project:dataset.table')
    dd = DisplayData.create_from(source)
    expected_items = [
        DisplayDataItemMatcher('validation',
                               False),
        DisplayDataItemMatcher('table',
                               'xyz.com:project:dataset.table')]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

  def test_parse_table_reference(self):
    source = beam.io.BigQuerySource('dataset.table')
    self.assertEqual(source.table_reference.datasetId, 'dataset')
    self.assertEqual(source.table_reference.tableId, 'table')

    source = beam.io.BigQuerySource('project:dataset.table')
    self.assertEqual(source.table_reference.projectId, 'project')
    self.assertEqual(source.table_reference.datasetId, 'dataset')
    self.assertEqual(source.table_reference.tableId, 'table')

    source = beam.io.BigQuerySource('xyz.com:project:dataset.table')
    self.assertEqual(source.table_reference.projectId, 'xyz.com:project')
    self.assertEqual(source.table_reference.datasetId, 'dataset')
    self.assertEqual(source.table_reference.tableId, 'table')

    source = beam.io.BigQuerySource(query='my_query')
    self.assertEqual(source.query, 'my_query')
    self.assertIsNone(source.table_reference)
    self.assertTrue(source.use_legacy_sql)

  def test_query_only_display_data(self):
    source = beam.io.BigQuerySource(query='my_query')
    dd = DisplayData.create_from(source)
    expected_items = [
        DisplayDataItemMatcher('validation', False),
        DisplayDataItemMatcher('query', 'my_query')]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

  def test_specify_query_sql_format(self):
    source = beam.io.BigQuerySource(query='my_query', use_standard_sql=True)
    self.assertEqual(source.query, 'my_query')
    self.assertFalse(source.use_legacy_sql)

  def test_specify_query_flattened_records(self):
    source = beam.io.BigQuerySource(query='my_query', flatten_results=False)
    self.assertFalse(source.flatten_results)

  def test_specify_query_unflattened_records(self):
    source = beam.io.BigQuerySource(query='my_query', flatten_results=True)
    self.assertTrue(source.flatten_results)

  def test_specify_query_without_table(self):
    source = beam.io.BigQuerySource(query='my_query')
    self.assertEqual(source.query, 'my_query')
    self.assertIsNone(source.table_reference)

  def test_date_partitioned_table_name(self):
    source = beam.io.BigQuerySource('dataset.table$20030102', validate=True)
    dd = DisplayData.create_from(source)
    expected_items = [
        DisplayDataItemMatcher('validation', True),
        DisplayDataItemMatcher('table', 'dataset.table$20030102')]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))


@unittest.skipIf(HttpError is None, 'GCP dependencies are not installed')
class TestBigQuerySink(unittest.TestCase):

  def test_table_spec_display_data(self):
    sink = beam.io.BigQuerySink('dataset.table')
    dd = DisplayData.create_from(sink)
    expected_items = [
        DisplayDataItemMatcher('table', 'dataset.table'),
        DisplayDataItemMatcher('validation', False)]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

  def test_parse_schema_descriptor(self):
    sink = beam.io.BigQuerySink(
        'dataset.table', schema='s:STRING, n:INTEGER')
    self.assertEqual(sink.table_reference.datasetId, 'dataset')
    self.assertEqual(sink.table_reference.tableId, 'table')
    result_schema = {
        field.name: field.type for field in sink.table_schema.fields}
    self.assertEqual({'n': 'INTEGER', 's': 'STRING'}, result_schema)

  def test_project_table_display_data(self):
    sinkq = beam.io.BigQuerySink('PROJECT:dataset.table')
    dd = DisplayData.create_from(sinkq)
    expected_items = [
        DisplayDataItemMatcher('table', 'PROJECT:dataset.table'),
        DisplayDataItemMatcher('validation', False)]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

  def test_simple_schema_as_json(self):
    sink = beam.io.BigQuerySink(
        'PROJECT:dataset.table', schema='s:STRING, n:INTEGER')
    self.assertEqual(
        json.dumps({'fields': [
            {'name': 's', 'type': 'STRING', 'mode': 'NULLABLE'},
            {'name': 'n', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}),
        sink.schema_as_json())

  def test_nested_schema_as_json(self):
    string_field = bigquery.TableFieldSchema(
        name='s', type='STRING', mode='NULLABLE', description='s description')
    number_field = bigquery.TableFieldSchema(
        name='n', type='INTEGER', mode='REQUIRED', description='n description')
    record_field = bigquery.TableFieldSchema(
        name='r', type='RECORD', mode='REQUIRED', description='r description',
        fields=[string_field, number_field])
    schema = bigquery.TableSchema(fields=[record_field])
    sink = beam.io.BigQuerySink('dataset.table', schema=schema)
    self.assertEqual(
        {'fields': [
            {'name': 'r', 'type': 'RECORD', 'mode': 'REQUIRED',
             'description': 'r description', 'fields': [
                 {'name': 's', 'type': 'STRING', 'mode': 'NULLABLE',
                  'description': 's description'},
                 {'name': 'n', 'type': 'INTEGER', 'mode': 'REQUIRED',
                  'description': 'n description'}]}]},
        json.loads(sink.schema_as_json()))


@unittest.skipIf(HttpError is None, 'GCP dependencies are not installed')
class TestBigQueryReader(unittest.TestCase):

  def get_test_rows(self):
    now = time.time()
    dt = datetime.datetime.utcfromtimestamp(float(now))
    ts = dt.strftime('%Y-%m-%d %H:%M:%S.%f UTC')
    expected_rows = [
        {
            'i': 1,
            's': 'abc',
            'f': 2.3,
            'b': True,
            't': ts,
            'dt': '2016-10-31',
            'ts': '22:39:12.627498',
            'dt_ts': '2008-12-25T07:30:00',
            'r': {'s2': 'b'},
            'rpr': [{'s3': 'c', 'rpr2': [{'rs': ['d', 'e'], 's4': None}]}]
        },
        {
            'i': 10,
            's': 'xyz',
            'f': -3.14,
            'b': False,
            'rpr': [],
            't': None,
            'dt': None,
            'ts': None,
            'dt_ts': None,
            'r': None,
        }]

    nested_schema = [
        bigquery.TableFieldSchema(
            name='s2', type='STRING', mode='NULLABLE')]
    nested_schema_2 = [
        bigquery.TableFieldSchema(
            name='s3', type='STRING', mode='NULLABLE'),
        bigquery.TableFieldSchema(
            name='rpr2', type='RECORD', mode='REPEATED', fields=[
                bigquery.TableFieldSchema(
                    name='rs', type='STRING', mode='REPEATED'),
                bigquery.TableFieldSchema(
                    name='s4', type='STRING', mode='NULLABLE')])]

    schema = bigquery.TableSchema(
        fields=[
            bigquery.TableFieldSchema(
                name='b', type='BOOLEAN', mode='REQUIRED'),
            bigquery.TableFieldSchema(
                name='f', type='FLOAT', mode='REQUIRED'),
            bigquery.TableFieldSchema(
                name='i', type='INTEGER', mode='REQUIRED'),
            bigquery.TableFieldSchema(
                name='s', type='STRING', mode='REQUIRED'),
            bigquery.TableFieldSchema(
                name='t', type='TIMESTAMP', mode='NULLABLE'),
            bigquery.TableFieldSchema(
                name='dt', type='DATE', mode='NULLABLE'),
            bigquery.TableFieldSchema(
                name='ts', type='TIME', mode='NULLABLE'),
            bigquery.TableFieldSchema(
                name='dt_ts', type='DATETIME', mode='NULLABLE'),
            bigquery.TableFieldSchema(
                name='r', type='RECORD', mode='NULLABLE',
                fields=nested_schema),
            bigquery.TableFieldSchema(
                name='rpr', type='RECORD', mode='REPEATED',
                fields=nested_schema_2)])

    table_rows = [
        bigquery.TableRow(f=[
            bigquery.TableCell(v=to_json_value('true')),
            bigquery.TableCell(v=to_json_value(str(2.3))),
            bigquery.TableCell(v=to_json_value(str(1))),
            bigquery.TableCell(v=to_json_value('abc')),
            # For timestamps cannot use str() because it will truncate the
            # number representing the timestamp.
            bigquery.TableCell(v=to_json_value('%f' % now)),
            bigquery.TableCell(v=to_json_value('2016-10-31')),
            bigquery.TableCell(v=to_json_value('22:39:12.627498')),
            bigquery.TableCell(v=to_json_value('2008-12-25T07:30:00')),
            # For record we cannot use dict because it doesn't create nested
            # schemas correctly so we have to use this f,v based format
            bigquery.TableCell(v=to_json_value({'f': [{'v': 'b'}]})),
            bigquery.TableCell(v=to_json_value([{'v':{'f':[{'v': 'c'}, {'v':[
                {'v':{'f':[{'v':[{'v':'d'}, {'v':'e'}]}, {'v':None}]}}]}]}}]))
            ]),
        bigquery.TableRow(f=[
            bigquery.TableCell(v=to_json_value('false')),
            bigquery.TableCell(v=to_json_value(str(-3.14))),
            bigquery.TableCell(v=to_json_value(str(10))),
            bigquery.TableCell(v=to_json_value('xyz')),
            bigquery.TableCell(v=None),
            bigquery.TableCell(v=None),
            bigquery.TableCell(v=None),
            bigquery.TableCell(v=None),
            bigquery.TableCell(v=None),
            # REPEATED field without any values.
            bigquery.TableCell(v=None)])]
    return table_rows, schema, expected_rows

  def test_read_from_table(self):
    client = mock.Mock()
    client.jobs.Insert.return_value = bigquery.Job(
        jobReference=bigquery.JobReference(
            jobId='somejob'))
    table_rows, schema, expected_rows = self.get_test_rows()
    client.jobs.GetQueryResults.return_value = bigquery.GetQueryResultsResponse(
        jobComplete=True, rows=table_rows, schema=schema)
    actual_rows = []
    with beam.io.BigQuerySource('dataset.table').reader(client) as reader:
      for row in reader:
        actual_rows.append(row)
    self.assertEqual(actual_rows, expected_rows)
    self.assertEqual(schema, reader.schema)

  def test_read_from_query(self):
    client = mock.Mock()
    client.jobs.Insert.return_value = bigquery.Job(
        jobReference=bigquery.JobReference(
            jobId='somejob'))
    table_rows, schema, expected_rows = self.get_test_rows()
    client.jobs.GetQueryResults.return_value = bigquery.GetQueryResultsResponse(
        jobComplete=True, rows=table_rows, schema=schema)
    actual_rows = []
    with beam.io.BigQuerySource(query='query').reader(client) as reader:
      for row in reader:
        actual_rows.append(row)
    self.assertEqual(actual_rows, expected_rows)
    self.assertEqual(schema, reader.schema)
    self.assertTrue(reader.use_legacy_sql)
    self.assertTrue(reader.flatten_results)

  def test_read_from_query_sql_format(self):
    client = mock.Mock()
    client.jobs.Insert.return_value = bigquery.Job(
        jobReference=bigquery.JobReference(
            jobId='somejob'))
    table_rows, schema, expected_rows = self.get_test_rows()
    client.jobs.GetQueryResults.return_value = bigquery.GetQueryResultsResponse(
        jobComplete=True, rows=table_rows, schema=schema)
    actual_rows = []
    with beam.io.BigQuerySource(
        query='query', use_standard_sql=True).reader(client) as reader:
      for row in reader:
        actual_rows.append(row)
    self.assertEqual(actual_rows, expected_rows)
    self.assertEqual(schema, reader.schema)
    self.assertFalse(reader.use_legacy_sql)
    self.assertTrue(reader.flatten_results)

  def test_read_from_query_unflatten_records(self):
    client = mock.Mock()
    client.jobs.Insert.return_value = bigquery.Job(
        jobReference=bigquery.JobReference(
            jobId='somejob'))
    table_rows, schema, expected_rows = self.get_test_rows()
    client.jobs.GetQueryResults.return_value = bigquery.GetQueryResultsResponse(
        jobComplete=True, rows=table_rows, schema=schema)
    actual_rows = []
    with beam.io.BigQuerySource(
        query='query', flatten_results=False).reader(client) as reader:
      for row in reader:
        actual_rows.append(row)
    self.assertEqual(actual_rows, expected_rows)
    self.assertEqual(schema, reader.schema)
    self.assertTrue(reader.use_legacy_sql)
    self.assertFalse(reader.flatten_results)

  def test_using_both_query_and_table_fails(self):
    with self.assertRaisesRegexp(
        ValueError,
        r'Both a BigQuery table and a query were specified\. Please specify '
        r'only one of these'):
      beam.io.BigQuerySource(table='dataset.table', query='query')

  def test_using_neither_query_nor_table_fails(self):
    with self.assertRaisesRegexp(
        ValueError, r'A BigQuery table or a query must be specified'):
      beam.io.BigQuerySource()

  def test_read_from_table_as_tablerows(self):
    client = mock.Mock()
    client.jobs.Insert.return_value = bigquery.Job(
        jobReference=bigquery.JobReference(
            jobId='somejob'))
    table_rows, schema, _ = self.get_test_rows()
    client.jobs.GetQueryResults.return_value = bigquery.GetQueryResultsResponse(
        jobComplete=True, rows=table_rows, schema=schema)
    actual_rows = []
    # We set the coder to TableRowJsonCoder, which is a signal that
    # the caller wants to see the rows as TableRows.
    with beam.io.BigQuerySource(
        'dataset.table', coder=TableRowJsonCoder).reader(client) as reader:
      for row in reader:
        actual_rows.append(row)
    self.assertEqual(actual_rows, table_rows)
    self.assertEqual(schema, reader.schema)

  @mock.patch('time.sleep', return_value=None)
  def test_read_from_table_and_job_complete_retry(self, patched_time_sleep):
    client = mock.Mock()
    client.jobs.Insert.return_value = bigquery.Job(
        jobReference=bigquery.JobReference(
            jobId='somejob'))
    table_rows, schema, expected_rows = self.get_test_rows()
    # Return jobComplete=False on first call to trigger the code path where
    # query needs to handle waiting a bit.
    client.jobs.GetQueryResults.side_effect = [
        bigquery.GetQueryResultsResponse(
            jobComplete=False),
        bigquery.GetQueryResultsResponse(
            jobComplete=True, rows=table_rows, schema=schema)]
    actual_rows = []
    with beam.io.BigQuerySource('dataset.table').reader(client) as reader:
      for row in reader:
        actual_rows.append(row)
    self.assertEqual(actual_rows, expected_rows)

  def test_read_from_table_and_multiple_pages(self):
    client = mock.Mock()
    client.jobs.Insert.return_value = bigquery.Job(
        jobReference=bigquery.JobReference(
            jobId='somejob'))
    table_rows, schema, expected_rows = self.get_test_rows()
    # Return a pageToken on first call to trigger the code path where
    # query needs to handle multiple pages of results.
    client.jobs.GetQueryResults.side_effect = [
        bigquery.GetQueryResultsResponse(
            jobComplete=True, rows=table_rows, schema=schema,
            pageToken='token'),
        bigquery.GetQueryResultsResponse(
            jobComplete=True, rows=table_rows, schema=schema)]
    actual_rows = []
    with beam.io.BigQuerySource('dataset.table').reader(client) as reader:
      for row in reader:
        actual_rows.append(row)
    # We return expected rows for each of the two pages of results so we
    # adjust our expectation below accordingly.
    self.assertEqual(actual_rows, expected_rows * 2)

  def test_table_schema_without_project(self):
    # Reader should pick executing project by default.
    source = beam.io.BigQuerySource(table='mydataset.mytable')
    options = PipelineOptions(flags=['--project', 'myproject'])
    source.pipeline_options = options
    reader = source.reader()
    self.assertEquals('SELECT * FROM [myproject:mydataset.mytable];',
                      reader.query)


@unittest.skipIf(HttpError is None, 'GCP dependencies are not installed')
class TestBigQueryWriter(unittest.TestCase):

  @mock.patch('time.sleep', return_value=None)
  def test_no_table_and_create_never(self, patched_time_sleep):
    client = mock.Mock()
    client.tables.Get.side_effect = HttpError(
        response={'status': '404'}, url='', content='')
    create_disposition = beam.io.BigQueryDisposition.CREATE_NEVER
    with self.assertRaisesRegexp(
        RuntimeError, r'Table project:dataset\.table not found but create '
                      r'disposition is CREATE_NEVER'):
      with beam.io.BigQuerySink(
          'project:dataset.table',
          create_disposition=create_disposition).writer(client):
        pass

  def test_no_table_and_create_if_needed(self):
    client = mock.Mock()
    table = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project', datasetId='dataset', tableId='table'),
        schema=bigquery.TableSchema())
    client.tables.Get.side_effect = HttpError(
        response={'status': '404'}, url='', content='')
    client.tables.Insert.return_value = table
    create_disposition = beam.io.BigQueryDisposition.CREATE_IF_NEEDED
    with beam.io.BigQuerySink(
        'project:dataset.table',
        schema='somefield:INTEGER',
        create_disposition=create_disposition).writer(client):
      pass
    self.assertTrue(client.tables.Get.called)
    self.assertTrue(client.tables.Insert.called)

  @mock.patch('time.sleep', return_value=None)
  def test_no_table_and_create_if_needed_and_no_schema(
      self, patched_time_sleep):
    client = mock.Mock()
    client.tables.Get.side_effect = HttpError(
        response={'status': '404'}, url='', content='')
    create_disposition = beam.io.BigQueryDisposition.CREATE_IF_NEEDED
    with self.assertRaisesRegexp(
        RuntimeError, r'Table project:dataset\.table requires a schema\. None '
                      r'can be inferred because the table does not exist'):
      with beam.io.BigQuerySink(
          'project:dataset.table',
          create_disposition=create_disposition).writer(client):
        pass

  @mock.patch('time.sleep', return_value=None)
  def test_table_not_empty_and_write_disposition_empty(
      self, patched_time_sleep):
    client = mock.Mock()
    client.tables.Get.return_value = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project', datasetId='dataset', tableId='table'),
        schema=bigquery.TableSchema())
    client.tabledata.List.return_value = bigquery.TableDataList(totalRows=1)
    write_disposition = beam.io.BigQueryDisposition.WRITE_EMPTY
    with self.assertRaisesRegexp(
        RuntimeError, r'Table project:dataset\.table is not empty but write '
                      r'disposition is WRITE_EMPTY'):
      with beam.io.BigQuerySink(
          'project:dataset.table',
          write_disposition=write_disposition).writer(client):
        pass

  def test_table_empty_and_write_disposition_empty(self):
    client = mock.Mock()
    table = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project', datasetId='dataset', tableId='table'),
        schema=bigquery.TableSchema())
    client.tables.Get.return_value = table
    client.tabledata.List.return_value = bigquery.TableDataList(totalRows=0)
    client.tables.Insert.return_value = table
    write_disposition = beam.io.BigQueryDisposition.WRITE_EMPTY
    with beam.io.BigQuerySink(
        'project:dataset.table',
        write_disposition=write_disposition).writer(client):
      pass
    self.assertTrue(client.tables.Get.called)
    self.assertTrue(client.tabledata.List.called)
    self.assertFalse(client.tables.Delete.called)
    self.assertFalse(client.tables.Insert.called)

  @mock.patch('time.sleep', return_value=None)
  def test_table_with_write_disposition_truncate(self, _patched_sleep):
    client = mock.Mock()
    table = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project', datasetId='dataset', tableId='table'),
        schema=bigquery.TableSchema())
    client.tables.Get.return_value = table
    client.tables.Insert.return_value = table
    write_disposition = beam.io.BigQueryDisposition.WRITE_TRUNCATE
    with beam.io.BigQuerySink(
        'project:dataset.table',
        write_disposition=write_disposition).writer(client):
      pass
    self.assertTrue(client.tables.Get.called)
    self.assertTrue(client.tables.Delete.called)
    self.assertTrue(client.tables.Insert.called)

  def test_table_with_write_disposition_append(self):
    client = mock.Mock()
    table = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project', datasetId='dataset', tableId='table'),
        schema=bigquery.TableSchema())
    client.tables.Get.return_value = table
    client.tables.Insert.return_value = table
    write_disposition = beam.io.BigQueryDisposition.WRITE_APPEND
    with beam.io.BigQuerySink(
        'project:dataset.table',
        write_disposition=write_disposition).writer(client):
      pass
    self.assertTrue(client.tables.Get.called)
    self.assertFalse(client.tables.Delete.called)
    self.assertFalse(client.tables.Insert.called)

  def test_rows_are_written(self):
    client = mock.Mock()
    table = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project', datasetId='dataset', tableId='table'),
        schema=bigquery.TableSchema())
    client.tables.Get.return_value = table
    write_disposition = beam.io.BigQueryDisposition.WRITE_APPEND

    insert_response = mock.Mock()
    insert_response.insertErrors = []
    client.tabledata.InsertAll.return_value = insert_response

    with beam.io.BigQuerySink(
        'project:dataset.table',
        write_disposition=write_disposition).writer(client) as writer:
      writer.Write({'i': 1, 'b': True, 's': 'abc', 'f': 3.14})

    sample_row = {'i': 1, 'b': True, 's': 'abc', 'f': 3.14}
    expected_rows = []
    json_object = bigquery.JsonObject()
    for k, v in iteritems(sample_row):
      json_object.additionalProperties.append(
          bigquery.JsonObject.AdditionalProperty(
              key=k, value=to_json_value(v)))
    expected_rows.append(
        bigquery.TableDataInsertAllRequest.RowsValueListEntry(
            insertId='_1',  # First row ID generated with prefix ''
            json=json_object))
    client.tabledata.InsertAll.assert_called_with(
        bigquery.BigqueryTabledataInsertAllRequest(
            projectId='project', datasetId='dataset', tableId='table',
            tableDataInsertAllRequest=bigquery.TableDataInsertAllRequest(
                rows=expected_rows)))

  def test_table_schema_without_project(self):
    # Writer should pick executing project by default.
    sink = beam.io.BigQuerySink(table='mydataset.mytable')
    options = PipelineOptions(flags=['--project', 'myproject'])
    sink.pipeline_options = options
    writer = sink.writer()
    self.assertEquals('myproject', writer.project_id)


@unittest.skipIf(HttpError is None, 'GCP dependencies are not installed')
class TestBigQueryWrapper(unittest.TestCase):

  def test_delete_non_existing_dataset(self):
    client = mock.Mock()
    client.datasets.Delete.side_effect = HttpError(
        response={'status': '404'}, url='', content='')
    wrapper = beam.io.gcp.bigquery.BigQueryWrapper(client)
    wrapper._delete_dataset('', '')
    self.assertTrue(client.datasets.Delete.called)

  @mock.patch('time.sleep', return_value=None)
  def test_delete_dataset_retries_fail(self, patched_time_sleep):
    client = mock.Mock()
    client.datasets.Delete.side_effect = ValueError("Cannot delete")
    wrapper = beam.io.gcp.bigquery.BigQueryWrapper(client)
    with self.assertRaises(ValueError):
      wrapper._delete_dataset('', '')
    self.assertEqual(
        beam.io.gcp.bigquery.MAX_RETRIES + 1,
        client.datasets.Delete.call_count)
    self.assertTrue(client.datasets.Delete.called)

  def test_delete_non_existing_table(self):
    client = mock.Mock()
    client.tables.Delete.side_effect = HttpError(
        response={'status': '404'}, url='', content='')
    wrapper = beam.io.gcp.bigquery.BigQueryWrapper(client)
    wrapper._delete_table('', '', '')
    self.assertTrue(client.tables.Delete.called)

  @mock.patch('time.sleep', return_value=None)
  def test_delete_table_retries_fail(self, patched_time_sleep):
    client = mock.Mock()
    client.tables.Delete.side_effect = ValueError("Cannot delete")
    wrapper = beam.io.gcp.bigquery.BigQueryWrapper(client)
    with self.assertRaises(ValueError):
      wrapper._delete_table('', '', '')
    self.assertTrue(client.tables.Delete.called)

  @mock.patch('time.sleep', return_value=None)
  def test_delete_dataset_retries_for_timeouts(self, patched_time_sleep):
    client = mock.Mock()
    client.datasets.Delete.side_effect = [
        HttpError(
            response={'status': '408'}, url='', content=''),
        bigquery.BigqueryDatasetsDeleteResponse()
    ]
    wrapper = beam.io.gcp.bigquery.BigQueryWrapper(client)
    wrapper._delete_dataset('', '')
    self.assertTrue(client.datasets.Delete.called)

  @mock.patch('time.sleep', return_value=None)
  def test_delete_table_retries_for_timeouts(self, patched_time_sleep):
    client = mock.Mock()
    client.tables.Delete.side_effect = [
        HttpError(
            response={'status': '408'}, url='', content=''),
        bigquery.BigqueryTablesDeleteResponse()
    ]
    wrapper = beam.io.gcp.bigquery.BigQueryWrapper(client)
    wrapper._delete_table('', '', '')
    self.assertTrue(client.tables.Delete.called)

  @mock.patch('time.sleep', return_value=None)
  def test_temporary_dataset_is_unique(self, patched_time_sleep):
    client = mock.Mock()
    client.datasets.Get.return_value = bigquery.Dataset(
        datasetReference=bigquery.DatasetReference(
            projectId='project_id', datasetId='dataset_id'))
    wrapper = beam.io.gcp.bigquery.BigQueryWrapper(client)
    with self.assertRaises(RuntimeError):
      wrapper.create_temporary_dataset('project_id', 'location')
    self.assertTrue(client.datasets.Get.called)

  def test_get_or_create_dataset_created(self):
    client = mock.Mock()
    client.datasets.Get.side_effect = HttpError(
        response={'status': '404'}, url='', content='')
    client.datasets.Insert.return_value = bigquery.Dataset(
        datasetReference=bigquery.DatasetReference(
            projectId='project_id', datasetId='dataset_id'))
    wrapper = beam.io.gcp.bigquery.BigQueryWrapper(client)
    new_dataset = wrapper.get_or_create_dataset('project_id', 'dataset_id')
    self.assertEqual(new_dataset.datasetReference.datasetId, 'dataset_id')

  def test_get_or_create_dataset_fetched(self):
    client = mock.Mock()
    client.datasets.Get.return_value = bigquery.Dataset(
        datasetReference=bigquery.DatasetReference(
            projectId='project_id', datasetId='dataset_id'))
    wrapper = beam.io.gcp.bigquery.BigQueryWrapper(client)
    new_dataset = wrapper.get_or_create_dataset('project_id', 'dataset_id')
    self.assertEqual(new_dataset.datasetReference.datasetId, 'dataset_id')


@unittest.skipIf(HttpError is None, 'GCP dependencies are not installed')
class WriteToBigQuery(unittest.TestCase):

  def test_dofn_client_start_bundle_called(self):
    client = mock.Mock()
    client.tables.Get.return_value = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project_id', datasetId='dataset_id', tableId='table_id'))
    create_disposition = beam.io.BigQueryDisposition.CREATE_NEVER
    write_disposition = beam.io.BigQueryDisposition.WRITE_APPEND
    schema = {'fields': [
        {'name': 'month', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}

    fn = beam.io.gcp.bigquery.BigQueryWriteFn(
        table_id='table_id',
        dataset_id='dataset_id',
        project_id='project_id',
        batch_size=2,
        schema=schema,
        create_disposition=create_disposition,
        write_disposition=write_disposition,
        test_client=client)

    fn.start_bundle()
    self.assertTrue(client.tables.Get.called)

  def test_dofn_client_start_bundle_create_called(self):
    client = mock.Mock()
    client.tables.Get.return_value = None
    client.tables.Insert.return_value = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project_id', datasetId='dataset_id', tableId='table_id'))
    create_disposition = beam.io.BigQueryDisposition.CREATE_NEVER
    write_disposition = beam.io.BigQueryDisposition.WRITE_APPEND
    schema = {'fields': [
        {'name': 'month', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}

    fn = beam.io.gcp.bigquery.BigQueryWriteFn(
        table_id='table_id',
        dataset_id='dataset_id',
        project_id='project_id',
        batch_size=2,
        schema=schema,
        create_disposition=create_disposition,
        write_disposition=write_disposition,
        test_client=client)

    fn.start_bundle()
    self.assertTrue(client.tables.Get.called)
    self.assertTrue(client.tables.Insert.called)

  def test_dofn_client_process_performs_batching(self):
    client = mock.Mock()
    client.tables.Get.return_value = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project_id', datasetId='dataset_id', tableId='table_id'))
    client.tabledata.InsertAll.return_value = \
        bigquery.TableDataInsertAllResponse(insertErrors=[])
    create_disposition = beam.io.BigQueryDisposition.CREATE_NEVER
    write_disposition = beam.io.BigQueryDisposition.WRITE_APPEND
    schema = {'fields': [
        {'name': 'month', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}

    fn = beam.io.gcp.bigquery.BigQueryWriteFn(
        table_id='table_id',
        dataset_id='dataset_id',
        project_id='project_id',
        batch_size=2,
        schema=schema,
        create_disposition=create_disposition,
        write_disposition=write_disposition,
        test_client=client)

    fn.start_bundle()
    fn.process({'month': 1})

    self.assertTrue(client.tables.Get.called)
    # InsertRows not called as batch size is not hit yet
    self.assertFalse(client.tabledata.InsertAll.called)

  def test_dofn_client_process_flush_called(self):
    client = mock.Mock()
    client.tables.Get.return_value = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project_id', datasetId='dataset_id', tableId='table_id'))
    client.tabledata.InsertAll.return_value = (
        bigquery.TableDataInsertAllResponse(insertErrors=[]))
    create_disposition = beam.io.BigQueryDisposition.CREATE_NEVER
    write_disposition = beam.io.BigQueryDisposition.WRITE_APPEND
    schema = {'fields': [
        {'name': 'month', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}

    fn = beam.io.gcp.bigquery.BigQueryWriteFn(
        table_id='table_id',
        dataset_id='dataset_id',
        project_id='project_id',
        batch_size=2,
        schema=schema,
        create_disposition=create_disposition,
        write_disposition=write_disposition,
        test_client=client)

    fn.start_bundle()
    fn.process({'month': 1})
    fn.process({'month': 2})
    self.assertTrue(client.tables.Get.called)
    # InsertRows called as batch size is hit
    self.assertTrue(client.tabledata.InsertAll.called)

  def test_dofn_client_finish_bundle_flush_called(self):
    client = mock.Mock()
    client.tables.Get.return_value = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project_id', datasetId='dataset_id', tableId='table_id'))
    client.tabledata.InsertAll.return_value = \
        bigquery.TableDataInsertAllResponse(insertErrors=[])
    create_disposition = beam.io.BigQueryDisposition.CREATE_NEVER
    write_disposition = beam.io.BigQueryDisposition.WRITE_APPEND
    schema = {'fields': [
        {'name': 'month', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}

    fn = beam.io.gcp.bigquery.BigQueryWriteFn(
        table_id='table_id',
        dataset_id='dataset_id',
        project_id='project_id',
        batch_size=2,
        schema=schema,
        create_disposition=create_disposition,
        write_disposition=write_disposition,
        test_client=client)

    fn.start_bundle()
    fn.process({'month': 1})

    self.assertTrue(client.tables.Get.called)
    # InsertRows not called as batch size is not hit
    self.assertFalse(client.tabledata.InsertAll.called)

    fn.finish_bundle()
    # InsertRows called in finish bundle
    self.assertTrue(client.tabledata.InsertAll.called)

  def test_dofn_client_no_records(self):
    client = mock.Mock()
    client.tables.Get.return_value = bigquery.Table(
        tableReference=bigquery.TableReference(
            projectId='project_id', datasetId='dataset_id', tableId='table_id'))
    client.tabledata.InsertAll.return_value = \
        bigquery.TableDataInsertAllResponse(insertErrors=[])
    create_disposition = beam.io.BigQueryDisposition.CREATE_NEVER
    write_disposition = beam.io.BigQueryDisposition.WRITE_APPEND
    schema = {'fields': [
        {'name': 'month', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}

    fn = beam.io.gcp.bigquery.BigQueryWriteFn(
        table_id='table_id',
        dataset_id='dataset_id',
        project_id='project_id',
        batch_size=2,
        schema=schema,
        create_disposition=create_disposition,
        write_disposition=write_disposition,
        test_client=client)

    fn.start_bundle()
    self.assertTrue(client.tables.Get.called)
    # InsertRows not called as batch size is not hit
    self.assertFalse(client.tabledata.InsertAll.called)

    fn.finish_bundle()
    # InsertRows not called in finish bundle as no records
    self.assertFalse(client.tabledata.InsertAll.called)

  def test_noop_schema_parsing(self):
    expected_table_schema = None
    table_schema = beam.io.gcp.bigquery.BigQueryWriteFn.get_table_schema(
        schema=None)
    self.assertEqual(expected_table_schema, table_schema)

  def test_dict_schema_parsing(self):
    schema = {'fields': [
        {'name': 's', 'type': 'STRING', 'mode': 'NULLABLE'},
        {'name': 'n', 'type': 'INTEGER', 'mode': 'NULLABLE'},
        {'name': 'r', 'type': 'RECORD', 'mode': 'NULLABLE', 'fields': [
            {'name': 'x', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}]}
    table_schema = beam.io.gcp.bigquery.BigQueryWriteFn.get_table_schema(schema)
    string_field = bigquery.TableFieldSchema(
        name='s', type='STRING', mode='NULLABLE')
    nested_field = bigquery.TableFieldSchema(
        name='x', type='INTEGER', mode='NULLABLE')
    number_field = bigquery.TableFieldSchema(
        name='n', type='INTEGER', mode='NULLABLE')
    record_field = bigquery.TableFieldSchema(
        name='r', type='RECORD', mode='NULLABLE', fields=[nested_field])
    expected_table_schema = bigquery.TableSchema(
        fields=[string_field, number_field, record_field])
    self.assertEqual(expected_table_schema, table_schema)

  def test_string_schema_parsing(self):
    schema = 's:STRING, n:INTEGER'
    expected_dict_schema = {'fields': [
        {'name': 's', 'type': 'STRING', 'mode': 'NULLABLE'},
        {'name': 'n', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}
    dict_schema = (
        beam.io.gcp.bigquery.WriteToBigQuery.get_dict_table_schema(schema))
    self.assertEqual(expected_dict_schema, dict_schema)

  def test_table_schema_parsing(self):
    string_field = bigquery.TableFieldSchema(
        name='s', type='STRING', mode='NULLABLE')
    nested_field = bigquery.TableFieldSchema(
        name='x', type='INTEGER', mode='NULLABLE')
    number_field = bigquery.TableFieldSchema(
        name='n', type='INTEGER', mode='NULLABLE')
    record_field = bigquery.TableFieldSchema(
        name='r', type='RECORD', mode='NULLABLE', fields=[nested_field])
    schema = bigquery.TableSchema(
        fields=[string_field, number_field, record_field])
    expected_dict_schema = {'fields': [
        {'name': 's', 'type': 'STRING', 'mode': 'NULLABLE'},
        {'name': 'n', 'type': 'INTEGER', 'mode': 'NULLABLE'},
        {'name': 'r', 'type': 'RECORD', 'mode': 'NULLABLE', 'fields': [
            {'name': 'x', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}]}
    dict_schema = (
        beam.io.gcp.bigquery.WriteToBigQuery.get_dict_table_schema(schema))
    self.assertEqual(expected_dict_schema, dict_schema)

  def test_table_schema_parsing_end_to_end(self):
    string_field = bigquery.TableFieldSchema(
        name='s', type='STRING', mode='NULLABLE')
    nested_field = bigquery.TableFieldSchema(
        name='x', type='INTEGER', mode='NULLABLE')
    number_field = bigquery.TableFieldSchema(
        name='n', type='INTEGER', mode='NULLABLE')
    record_field = bigquery.TableFieldSchema(
        name='r', type='RECORD', mode='NULLABLE', fields=[nested_field])
    schema = bigquery.TableSchema(
        fields=[string_field, number_field, record_field])
    table_schema = beam.io.gcp.bigquery.BigQueryWriteFn.get_table_schema(
        beam.io.gcp.bigquery.WriteToBigQuery.get_dict_table_schema(schema))
    self.assertEqual(table_schema, schema)

  def test_none_schema_parsing(self):
    schema = None
    expected_dict_schema = None
    dict_schema = (
        beam.io.gcp.bigquery.WriteToBigQuery.get_dict_table_schema(schema))
    self.assertEqual(expected_dict_schema, dict_schema)

  def test_noop_dict_schema_parsing(self):
    schema = {'fields': [
        {'name': 's', 'type': 'STRING', 'mode': 'NULLABLE'},
        {'name': 'n', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}
    expected_dict_schema = schema
    dict_schema = (
        beam.io.gcp.bigquery.WriteToBigQuery.get_dict_table_schema(schema))
    self.assertEqual(expected_dict_schema, dict_schema)


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  unittest.main()
