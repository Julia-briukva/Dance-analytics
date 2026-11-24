import argparse
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_spb_database as crawler
import weekly_compreg as weekly
import weekly_state
from parse_protocols import init_schema


class DiscoveryTests(unittest.TestCase):
    def test_live_link_format_deduplicates_and_filters_four_weeks(self):
        html = '''<div onclick='LoadResults("2026112200",1)'></div>
          <a href="/resultsdata/2026/11/2200/listcat.php">Tula</a>
          <div onclick='LoadResults("2026100100",1)'></div>
          <div onclick='LoadResults("2026112500",1)'></div>'''
        self.assertEqual(weekly.discover(html, date(2026,11,24),28), ['https://compreg.ru/resultsdata/2026/11/2200/listcat.php'])

    def test_format_change_fails_instead_of_reporting_no_results(self):
        with self.assertRaises(ValueError):
            weekly.discover('<html>maintenance</html>', date(2026,11,24),28)

    def test_pairs_match_individual_idds_and_names_are_separate(self):
        html = '<div class="prot-header-box"></div><div class="prot-table-name">Test Name</div><div class="prot-table-IDD">2020091 / 2010736</div>'
        ids, names = weekly.participant_info(html)
        self.assertEqual(ids, {'2020091','2010736'})
        self.assertIn('test name',names)
        self.assertNotIn('202009',ids)

    def test_tula_is_accepted_without_being_renamed_petersburg(self):
        with tempfile.TemporaryDirectory() as temp:
            html = '<p class="prot-caption">Фантазия-2026 (Тула)</p><p class="prot-header-text">22.11.26</p><div onclick="LoadCategoryRes(2026112200,12345)"></div>'
            with patch.object(weekly,'get_html',return_value=html), patch.object(crawler,'listcat_cache_path',return_value=Path(temp)/'listcat.php'):
                page=weekly.page_for_url('https://compreg.ru/resultsdata/2026/11/2200/listcat.php')
            self.assertIsInstance(page,crawler.TournamentPage)
            self.assertEqual(page.raw_detected_city,'Тула')
            self.assertNotEqual(page.normalized_city,'Санкт-Петербург')

    def test_missing_idd_uses_confirmed_name_club_city_only(self):
        html = '<div class="prot-header-box"></div><div class="prot-table-box"><div class="prot-table-num">602</div><div class="prot-table-name">Россошанская Дарья</div><div class="prot-table-IDD">&nbsp;</div><div class="prot-table-club">Триумф, Санкт-Петербург</div></div>'
        identities = {('россошанская дарья','триумф','санкт-петербург'): (42,{'2020091'})}
        ids, bindings = weekly.identify_participants(html,{'2020091'},identities)
        self.assertEqual(ids,{'2020091'})
        self.assertEqual(bindings,[('602',42,'2020091')])
        different_club = html.replace('Триумф,','Другой клуб,')
        self.assertEqual(weekly.identify_participants(different_club,{'2020091'},identities),(set(),[]))
        other_id = html.replace('&nbsp;','9999999')
        self.assertEqual(weekly.identify_participants(other_id,{'2020091'},identities),(set(),[]))

    def test_identity_collision_is_not_automatically_resolved(self):
        conn=sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE dancers(id INTEGER, name TEXT, club TEXT, city TEXT, external_ref TEXT)')
        conn.executemany('INSERT INTO dancers VALUES (?,?,?,?,?)',[(1,'Test','Club','City','2020091'),(2,'Test','Club','City','2010736')])
        self.assertEqual(weekly.confirmed_identities(conn,{'2020091','2010736'}),{})
        conn.close()


class TransactionTests(unittest.TestCase):
    def test_failed_refresh_restores_old_marks(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            db=root/'history.sqlite'
            crawler.init_db(db)
            page=crawler.TournamentPage(date(2026,11,22),0,'https://compreg.ru/resultsdata/2026/11/2200/listcat.php',root/'listcat.php',2026112200,'Фантазия (Тула)','Тула','Тула','Тула','title',(12345,))
            html='<div class="prot-header-box"><p class="prot-caption">Фантазия (Тула)</p><p class="prot-caption">Ювеналы St</p><p class="prot-header-text">22.11.26</p></div>'
            with sqlite3.connect(db) as conn, patch.object(crawler,'PROJECT_ROOT',root), patch.object(crawler,'protocol_cache_path',return_value=root/'data/cache/protocols/12345.php'):
                init_schema(conn)
                crawler.upsert_tournament(conn,page)
                crawler.upsert_protocol(conn,2026112200,12345,'https://compreg.ru/resultsdata/2026/11/2200/12345.php',None,200)
                pid=conn.execute('SELECT id FROM protocols').fetchone()[0]
                conn.execute("INSERT INTO dancers(name,club,city) VALUES ('Test','','')")
                conn.execute("INSERT INTO judges(name,city) VALUES ('Judge','')")
                conn.execute("INSERT INTO marks(protocol_id,dancer_id,judge_id,round_name,dance_name,mark_value) VALUES (?,1,1,'Финал','W','1')",(pid,))
                conn.commit()
                with patch.object(weekly,'parse_protocol',side_effect=ValueError('broken layout')):
                    with self.assertRaisesRegex(ValueError,'broken layout'):
                        weekly.import_protocol(conn,page,12345,html)
                self.assertEqual(conn.execute('SELECT mark_value FROM marks').fetchall(), [('1',)])

    def test_changed_protocol_replaces_rows_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);db=root/'history.sqlite';crawler.init_db(db)
            page=crawler.TournamentPage(date(2026,11,22),0,'https://compreg.ru/resultsdata/2026/11/2200/listcat.php',root/'listcat.php',2026112200,'Фантазия (Тула)','Тула','Тула','Тула','title',(12345,))
            html='<div class="prot-header-box"><p class="prot-caption">Фантазия (Тула)</p><p class="prot-caption">Ювеналы St</p><p class="prot-header-text">22.11.26</p></div>'
            def parsed(conn,record):
                conn.execute("INSERT INTO protocol_dancers(protocol_id,dancer_id,competitor_number,dancer_name,idd,place) VALUES (?,1,'11','Test','2020091','2')",(record.db_id,))
                conn.execute("INSERT INTO marks(protocol_id,dancer_id,judge_id,round_name,dance_name,mark_value) VALUES (?,1,1,'Финал','W','2')",(record.db_id,))
                conn.execute("INSERT INTO protocol_parse_status(protocol_id,status,message) VALUES (?,'parsed',NULL)",(record.db_id,))
                return 'parsed',1
            with sqlite3.connect(db) as conn, patch.object(crawler,'PROJECT_ROOT',root), patch.object(crawler,'protocol_cache_path',return_value=root/'data/cache/protocols/12345.php'), patch.object(weekly,'parse_protocol',side_effect=parsed):
                init_schema(conn)
                conn.execute("INSERT INTO dancers(name,club,city) VALUES ('Test','','')")
                conn.execute("INSERT INTO judges(name,city) VALUES ('Judge','')")
                weekly.import_protocol(conn,page,12345,html)
                weekly.import_protocol(conn,page,12345,html+' ')
                self.assertEqual(conn.execute('SELECT count(*) FROM marks').fetchone()[0],1)
                self.assertEqual(conn.execute('SELECT count(*) FROM protocol_dancers').fetchone()[0],1)
                self.assertEqual(conn.execute('SELECT mark_value FROM marks').fetchone()[0],'2')


if __name__ == '__main__':
    unittest.main()
