import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import build_club_reports as reports


class CoupleIndexTests(unittest.TestCase):
    def row(self,root,idd,name,status='success'):
        html=root/f'reports/dancer_{idd}_report.html';html.parent.mkdir(parents=True,exist_ok=True)
        html.write_text('<html>'+('Report '*30)+'</html>')
        data=html.with_suffix('.json');data.write_text(json.dumps({'dancer':{'name':name}}))
        return {'idd':idd,'name':name,'status':status,'report_html':str(html.relative_to(root)),'report_json':str(data.relative_to(root))}

    def test_pair_partners_share_one_link_and_both_idds(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            rows=[self.row(root,'2010683','Агапитов Ярослав / Дмитриева Анна'),self.row(root,'2010737','Агапитов Ярослав / Дмитриева Анна')]
            with patch.object(reports,'PROJECT_ROOT',root):
                entries=reports.index_entries(rows)
                page=reports.render_index('<main></main>',rows)
            self.assertEqual(len(entries),1)
            self.assertEqual(entries[0]['idd'],'2010683 / 2010737')
            soup=BeautifulSoup(page,'html.parser')
            self.assertEqual(len(soup.select('a.club-name-link')),1)
            self.assertEqual(soup.select_one('a.club-name-link')['href'],'dancer_2010683_report.html')
            self.assertEqual([n.text for n in soup.select('.summary-value')],['1','1','0'])
            self.assertNotIn('copy-idd',page)
            self.assertEqual(rows[0]['idd'],'2010683')  # Source roster isn't modified.

    def test_partner_order_and_case_do_not_create_another_couple(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            rows=[self.row(root,'1234567','Первый Иван / Вторая Анна'),self.row(root,'7654321','вторая анна / первый иван')]
            with patch.object(reports,'PROJECT_ROOT',root):self.assertEqual(len(reports.index_entries(rows)),1)

    def test_new_partner_and_solo_remain_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            rows=[self.row(root,'1234567','Первый Иван / Вторая Анна'),self.row(root,'2345678','Первый Иван / Третья Ольга'),self.row(root,'3456789','Первый Иван')]
            with patch.object(reports,'PROJECT_ROOT',root):self.assertEqual(len(reports.index_entries(rows)),3)

    def test_working_partner_report_wins_over_missing_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            rows=[self.row(root,'1234567','Первый Иван / Вторая Анна','no_data'),self.row(root,'7654321','Первый Иван / Вторая Анна')]
            with patch.object(reports,'PROJECT_ROOT',root):entry=reports.index_entries(rows)[0]
            self.assertEqual(entry['report_html'],'reports/dancer_7654321_report.html')
            self.assertEqual(entry['idd'],'1234567 / 7654321')

    def test_solo_partner_idd_is_shown_without_removing_solo_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            pair=self.row(root,'2011924','Попов Егор / Чистова Анна')
            pair['name']='Попов Егор'
            solo=self.row(root,'2010734','Чистова Анна')
            with patch.object(reports,'PROJECT_ROOT',root):entries=reports.index_entries([pair,solo])
            self.assertEqual(len(entries),2)
            self.assertIn('2010734 / 2011924',[row['idd'] for row in entries])
            self.assertIn('2010734',[row['idd'] for row in entries])


if __name__=='__main__':unittest.main()
