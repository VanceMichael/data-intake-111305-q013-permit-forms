import hashlib
import json
import unittest

from permit_forms.client import Client
from permit_forms.decoding import parse_page, records as decode_records
from permit_forms.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            self.assertEqual(records[1]["district"], records[2]["district"])
            self.assertNotEqual(records[0]["district"], records[1]["district"])
            self.assertTrue(all(record["required"] for record in records))
            self.assertEqual(len(parse_page(client.fetch("/feed").body).forms), 3)

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
