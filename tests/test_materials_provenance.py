import json
from pathlib import Path
import tempfile
import unittest

from video_localizer.materials import Materials,load_materials,_load_table


class MaterialProvenanceTests(unittest.TestCase):
    def test_existing_positional_materials_constructor_is_compatible(self):
        value=Materials({"a":["b"]},{"c":["d"]},{"portrait":"p.png"})
        self.assertEqual(value.provenance,{})
        self.assertEqual(value.references,{"portrait":"p.png"})

    def test_csv_evidence_keeps_original_row_headers_and_physical_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"approved.csv"
            path.write_text('source,target,note\nPLAY,시작,approved\n\n"LONG\nLABEL",긴 문구,reviewed\nPLAY,플레이,conflict\n',encoding="utf-8")
            material=load_materials(translations=path)
            self.assertEqual(material.translations["play"],["시작","플레이"])
            rows=material.provenance["translations"]["play"]
            self.assertEqual([r["line_number"] for r in rows],[2,6])
            self.assertEqual(rows[0]["raw_row"],["PLAY","시작","approved"])
            self.assertEqual(rows[0]["columns"],["source","target","note"])
            self.assertEqual(rows[0]["file"],str(path.resolve()))
            long=material.provenance["translations"]["long label"][0]
            self.assertEqual(long["line_number"],4)
            self.assertEqual(long["source"],"LONG\nLABEL")

    def test_json_record_number_does_not_pretend_to_be_a_physical_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"glossary.json"
            original={"glossary":[{"source":"PLAY","target":"시작","note":"confirmed"}]}
            path.write_text(json.dumps(original,ensure_ascii=False,indent=2),encoding="utf-8")
            material=load_materials(glossary=path)
            row=material.provenance["glossary"]["play"][0]
            self.assertEqual(row["row_number"],1)
            self.assertIsNone(row["line_number"])
            self.assertEqual(row["row_kind"],"json_record")
            self.assertEqual(row["raw_row"],original["glossary"][0])

    def test_no_header_table_and_old_loader_return_type_are_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"terms.tsv"
            path.write_text("PLAY\t시작\nGO\t가자\n",encoding="utf-8")
            self.assertEqual(_load_table(path,"glossary"),{"play":["시작"],"go":["가자"]})
            material=load_materials(glossary=path)
            self.assertEqual(material.provenance["glossary"]["go"][0]["line_number"],2)


if __name__=="__main__":
    unittest.main()
