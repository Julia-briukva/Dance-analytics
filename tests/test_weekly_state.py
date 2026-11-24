import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import build_spb_database as crawler
import weekly_state as state
from parse_protocols import init_schema


class StateTests(unittest.TestCase):
    def seed(self,root):
        db=root/state.DB_REL;db.parent.mkdir(parents=True,exist_ok=True);crawler.init_db(db)
        with sqlite3.connect(db) as conn:
            init_schema(conn)
            conn.execute("INSERT INTO tournaments(id,event_date,suffix,listcat_url,listcat_cache_path) VALUES (2026112200,'2026-11-22',0,'https://compreg.ru/resultsdata/2026/11/2200/listcat.php','data/cache/listcat/2026/11/2200/listcat.php')")
            conn.execute("INSERT INTO protocols(tournament_id,protocol_id,protocol_url,protocol_cache_path) VALUES (2026112200,12345,'https://compreg.ru/resultsdata/2026/11/2200/12345.php','data/cache/protocols/2026/11/2200/12345.php')")
            conn.execute("INSERT INTO dancers(name,club,city,external_ref) VALUES ('Test','','','2020091')")
            conn.execute("INSERT INTO judges(name,city) VALUES ('Judge','')")
            conn.execute("INSERT INTO marks(protocol_id,dancer_id,judge_id,round_name,dance_name,mark_value) VALUES (1,1,1,'Финал','W','2')")
        card=root/'data/cache/dancer_profiles/2020091_danceinfo_post_ci_2020091.html'
        card.parent.mkdir(parents=True);card.write_text('Стандарт D класс',encoding='utf-8')
        return db,card

    def test_roundtrip_preserves_history_and_assigned_class_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);src=tmp/'source';dest=tmp/'restored';checkpoint=tmp/'private/state'
            db,card=self.seed(src)
            state.save(checkpoint,src)
            state.restore(checkpoint,dest)
            self.assertEqual(state.validate(db),state.validate(dest/state.DB_REL))
            self.assertEqual((dest/card.relative_to(src)).read_text(),card.read_text())
            before=(checkpoint/'database.sqlite.gz').read_bytes()
            state.save(checkpoint,src)
            self.assertEqual((checkpoint/'database.sqlite.gz').read_bytes(),before)

    def test_bad_checksum_never_overwrites_existing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);src=tmp/'source';dest=tmp/'restored';checkpoint=tmp/'private/state'
            self.seed(src);state.save(checkpoint,src);state.restore(checkpoint,dest)
            target=dest/state.DB_REL;before=target.read_bytes()
            m=json.loads((checkpoint/'manifest.json').read_text());m['database_sha256']='0'*64
            (checkpoint/'manifest.json').write_text(json.dumps(m))
            with self.assertRaisesRegex(ValueError,'Контрольная сумма'):
                state.restore(checkpoint,dest)
            self.assertEqual(target.read_bytes(),before)

    def test_empty_database_cannot_replace_seed_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=root/state.DB_REL;db.parent.mkdir(parents=True,exist_ok=True);crawler.init_db(db)
            with sqlite3.connect(db) as conn:init_schema(conn)
            with self.assertRaisesRegex(ValueError,'пустую базу'):
                state.save(root/'state',root)


if __name__=='__main__':unittest.main()
