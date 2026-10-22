import json
import os
import tempfile
import unittest
from pathlib import Path

import gemmi
import requests

from pdb_homologs import search_homologs, download_homolog, read_fasta
from test_daq import make_structure


class Response:
    def __init__(self, payload=None, status=200, text=""):
        self.payload, self.status_code, self.text = payload, status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    get = post


class PdbHomologTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fasta = self.root / "assembly.fasta"
        self.fasta.write_text(">copy1\nACDEFGHIKLMN\n>copy2\nACDEFGHIKLMN\n")

    def test_search_deduplicates_assembly_and_excludes_computed_models(self):
        context = {"sequence_identity": .8, "evalue": 1e-10, "query_beg": 2, "query_end": 10, "bitscore": 50}
        session = Session([Response({"total_count": 1, "result_set": [{"identifier": "1CRN_1",
            "services": [{"service_type": "sequence", "nodes": [{"match_context": [context]}]}]}]})])
        result = search_homologs(self.fasta, self.root / "search", session=session)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]["json"]["request_options"]["results_content_type"], ["experimental"])
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["total_sequence_length"], 24)
        self.assertEqual(result["queries"][0]["copies"], 2)
        self.assertEqual(result["queries"][0]["hits"][0]["query_coverage"], .75)
        self.assertTrue(Path(result["report_path"]).exists())

    def test_empty_results_and_api_failure_are_distinct(self):
        result = search_homologs(self.fasta, self.root / "search", session=Session([Response(status=204)]))
        self.assertEqual(result["queries"][0]["status"], "no_hits")
        with self.assertRaises(requests.HTTPError):
            search_homologs(self.fasta, self.root / "error", session=Session([Response(status=503)]))
        self.assertFalse((self.root / "error/pdb_search.json").exists())
        with self.assertRaisesRegex(RuntimeError, "not a no-homolog result"):
            search_homologs(self.fasta, self.root / "error", session=Session([Response({"message": "error"})]))

    def test_download_extracts_one_correct_polymer_copy_and_model(self):
        structure = make_structure()
        for model in structure:
            for residue in model[0]:
                residue.subchain = "X"
            decoy = model[0].clone()
            decoy.name = "B"
            for residue in decoy:
                residue.subchain = "Y"
            model.add_chain(decoy)
        session = Session([
            Response({"entity_poly": {"rcsb_entity_polymer_type": "Protein"},
                      "rcsb_polymer_entity_container_identifiers": {"asym_ids": ["X"]}}),
            Response(text=structure.make_mmcif_document().as_string())])
        result = download_homolog("1CRN_1", self.root / "reference", session=session)
        actual = gemmi.read_structure(result["model_path"])
        self.assertEqual(len(actual), 1)
        self.assertEqual(len(actual[0]), 1)
        self.assertEqual(actual[0][0].name, "A0")
        self.assertEqual(result["resolved_residues"], 2)

    def test_invalid_fasta_and_nonpdb_identifiers_are_rejected(self):
        self.fasta.write_text(">bad\nACD-EF\n")
        with self.assertRaises(ValueError):
            read_fasta(self.fasta)
        with self.assertRaises(ValueError):
            download_homolog("AF-FAKE-F1", self.root / "reference", session=Session([]))

    @unittest.skipUnless(os.environ.get("CRYOAGENT_TEST_PDB") == "1", "optional public PDB API smoke test")
    def test_live_crambin_search_and_download(self):
        self.fasta.write_text(">crambin\nTTCCPSIVARSNFNVCRLPGTPEAICATYTGCIIIPGATCPGDYAN\n")
        result = search_homologs(self.fasta, self.root / "search", max_hits=2)
        hit = result["queries"][0]["hits"][0]
        self.assertGreaterEqual(hit["identity"], .95)
        reference = download_homolog(hit["entity_id"], self.root / "reference")
        self.assertGreater(reference["resolved_residues"], 40)


if __name__ == "__main__":
    unittest.main()
