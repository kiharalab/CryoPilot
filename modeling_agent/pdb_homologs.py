"""Experimental PDB sequence searches and single-polymer reference downloads.

API contracts: https://search.rcsb.org/ and https://data.rcsb.org/.
Search failures are deliberately distinct from a successful search with no hits.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import gemmi
import requests

SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
DATA_URL = "https://data.rcsb.org/rest/v1/core/polymer_entity"
ENTITY_ID = re.compile(r"([0-9][A-Za-z0-9]{3})_([1-9][0-9]*)\Z")


def read_fasta(path: Path) -> list[dict]:
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith(">"):
            records.append({"header": line[1:].strip(), "sequence": ""})
        elif not records:
            raise ValueError("FASTA sequence must follow a >header.")
        else:
            records[-1]["sequence"] += "".join(line.split()).upper()
    if not records:
        raise ValueError("FASTA contains no records.")
    for record in records:
        record["sequence"] = record["sequence"].removesuffix("*")
        if not record["header"] or not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWYBXZJUO]+", record["sequence"]):
            raise ValueError("Expected nonempty protein FASTA records without alignment gaps.")
    return records


def search_homologs(fasta: Path, destination: Path, *, identity_cutoff: float = 0.3,
                    max_hits: int = 5, session=None) -> dict:
    if not 0 <= identity_cutoff <= 1 or not 1 <= max_hits <= 20:
        raise ValueError("Identity must be in [0,1]; max_hits must be 1..20.")
    records = read_fasta(fasta)
    unique = {}
    for record in records:
        unique.setdefault(record["sequence"], []).append(record["header"])
    session = session or requests.Session()
    queries = []
    for sequence, headers in unique.items():
        query = {
            "query": {"type": "terminal", "service": "sequence", "parameters": {
                "evalue_cutoff": 1e-5, "identity_cutoff": identity_cutoff,
                "sequence_type": "protein", "value": sequence}},
            "return_type": "polymer_entity",
            "request_options": {"paginate": {"start": 0, "rows": max_hits},
                                "results_content_type": ["experimental"],
                                "results_verbosity": "verbose"},
        }
        response = session.post(SEARCH_URL, json=query, timeout=60)
        response.raise_for_status()
        payload = {} if response.status_code == 204 else response.json()
        if response.status_code != 204 and "result_set" not in payload:
            raise RuntimeError("RCSB returned no result_set; this is not a no-homolog result.")
        hits = []
        for hit in payload.get("result_set", []):
            identifier = hit["identifier"]
            if not ENTITY_ID.fullmatch(identifier):
                raise RuntimeError(f"Unexpected experimental PDB entity identifier: {identifier}")
            contexts = [context for service in hit.get("services", [])
                        if service.get("service_type") == "sequence"
                        for node in service.get("nodes", []) for context in node.get("match_context", [])]
            if not contexts:
                raise RuntimeError(f"RCSB omitted sequence alignment evidence for {identifier}.")
            match = max(contexts, key=lambda c: c.get("bitscore", 0))
            hits.append({"entity_id": identifier, "pdb_id": identifier.split("_")[0],
                         "identity": match["sequence_identity"], "evalue": match["evalue"],
                         "query_coverage": (match["query_end"] - match["query_beg"] + 1) / len(sequence),
                         "alignment": match, "rcsb_url": f"https://www.rcsb.org/structure/{identifier.split('_')[0]}"})
        hits.sort(key=lambda h: (h["query_coverage"] * h["identity"], -h["evalue"]), reverse=True)
        queries.append({"headers": headers, "copies": len(headers), "length": len(sequence),
                        "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                        "status": "hits" if hits else "no_hits", "hits": hits,
                        "total_hits": payload.get("total_count", 0)})
    result = {"ok": True, "database": "experimental PDB only", "fasta_path": str(fasta),
              "record_count": len(records), "unique_sequences": len(unique),
              "total_sequence_length": sum(len(r["sequence"]) for r in records),
              "identity_cutoff": identity_cutoff, "evalue_cutoff": 1e-5, "queries": queries,
              "note": "Ranked sequence hits are references to assess, not an established handedness decision."}
    destination.mkdir(parents=True, exist_ok=True)
    report = destination / "pdb_search.json"
    if report.resolve() == fasta.resolve():
        raise ValueError("Search report must not overwrite the input FASTA.")
    result["report_path"] = str(report.resolve())
    report.write_text(json.dumps(result, indent=2, allow_nan=False))
    return result


def download_homolog(entity_id: str, destination: Path, *, session=None) -> dict:
    match = ENTITY_ID.fullmatch(entity_id)
    if not match:
        raise ValueError("Use an experimental PDB polymer entity ID, e.g. 1CRN_1.")
    entry, entity = match.groups()
    entry = entry.upper()
    session = session or requests.Session()
    response = session.get(f"{DATA_URL}/{entry}/{entity}", timeout=60)
    response.raise_for_status()
    metadata = response.json()
    if metadata.get("entity_poly", {}).get("rcsb_entity_polymer_type") != "Protein":
        raise ValueError("The homolog reference must be a protein polymer entity.")
    asym_ids = metadata["rcsb_polymer_entity_container_identifiers"]["asym_ids"]
    url = f"https://files.rcsb.org/download/{entry}.cif"
    response = session.get(url, timeout=120)
    response.raise_for_status()
    structure = gemmi.make_structure_from_block(gemmi.cif.read_string(response.text).sole_block())
    if not len(structure):
        raise ValueError("Downloaded PDB entry contains no model coordinates.")
    # One polymer copy from the first coordinate model, not every chain in the entry.
    available = {r.subchain for c in structure[0] for r in c if len(r)}
    asym = next((name for name in asym_ids if name in available), None)
    if asym is None:
        raise ValueError("Requested PDB entity has no resolved coordinates.")
    if not re.fullmatch(r"[A-Za-z0-9]+", asym):
        raise ValueError("Unexpected PDB label_asym_id.")
    selected = structure.clone()
    for index in reversed(range(1, len(selected))):
        del selected[index]
    for chain_index in reversed(range(len(selected[0]))):
        chain = selected[0][chain_index]
        for residue_index in reversed(range(len(chain))):
            if chain[residue_index].subchain != asym:
                del chain[residue_index]
        if not len(chain):
            del selected[0][chain_index]
    selected.remove_hydrogens()
    selected.remove_alternative_conformations()
    destination.mkdir(parents=True, exist_ok=True)
    model_path = destination / f"{entry}_{entity}_{asym}.cif"
    selected.make_mmcif_document().write_file(str(model_path))
    result = {"ok": True, "entity_id": f"{entry}_{entity}", "label_asym_id": asym,
              "source_url": url, "model_path": str(model_path.resolve()),
              "resolved_residues": sum(len(c) for c in selected[0]),
              "metadata": metadata}
    (destination / "pdb_reference.json").write_text(json.dumps(result, indent=2))
    return result
