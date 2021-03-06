'''
Copyright © 2020 YutoWatanabe
'''
import os
import re
import time
from typing import Any, Dict

import click
import linebot
import requests
import xmltodict
from linebot.models import FlexSendMessage

from emergency_stop import stop
from json_operation import json_read, json_write
from report import format_report
from template import apply_template


@click.command()
@click.option('--line-token', 'token', prompt=True, hide_input=True, help='Line token')
def main(token: str):
    '''
    メイン。30秒ごとに実行します。

    Args:
        token (str): LINEのトークン
    '''
    run_directory = os.path.dirname(__file__)
    url = 'http://www.data.jma.go.jp/developer/xml/feed/eqvol.xml'
    earthquake = Earthquake(run_directory, url, token)
    while(True):  # pylint: disable=C0325
        if earthquake.check_update():
            earthquake.get_earthquake_information()
            earthquake.find_latest()
            earthquake.post_line()
        time.sleep(30)


class Earthquake():  # pylint: disable=R0902
    '''
    地震情報を取得、フォーマット、LINEにpostします。
    '''

    def __init__(self, save_directory: str, url: str, token: str):
        self.url = url
        self.token = token
        self.save_directory = save_directory
        self.directory = os.path.join(self.save_directory, 'saves')
        if not os.path.isdir(self.directory):
            os.makedirs(self.directory)

        self.responce: Any = None
        self.formated_text: Any = []
        self.xml_root: Any = None
        self.post_message: Any = []

    def check_update(self) -> bool:
        '''
        サイトが更新されているか確認します。

        Returns:
            bool: 更新されていた場合True。されていない場合はFalse。
        '''
        is_update = False
        try:
            self.responce = requests.get(self.url)
        except requests.exceptions.ConnectionError:
            return False
        last_acquisition_file_path = os.path.join(self.directory, 'last_acquisition.json')
        last_acquisition = self.__load_buffer(last_acquisition_file_path, {'latest': None})

        last_modified: str = self.responce.headers['Last-Modified']

        if last_modified != last_acquisition['latest']:
            is_update = True
            self.__save_buffer(last_acquisition_file_path, {'latest': last_modified})

        return is_update

    def get_earthquake_information(self):
        '''
        地震速報を取得します。
        - 震度速報
        - 震源に関する情報
        - 震源・震度に関する情報
        - 緊急地震速報(予報)
        - 緊急地震速報(警報)

        すべてをフォーマットします。
        '''
        self.formated_text = []

        assert self.responce is not None, 'Can not read page.'

        self.responce.encoding = 'UTF-8'
        text = self.responce.text

        try:
            self.xml_root = xmltodict.parse(text)
        except xmltodict.expat.ExpartError:
            return

        for child in self.xml_root['feed']['entry']:
            title = child['title']
            tsunami = re.search(r'津波', title)
            if title == '震度速報':
                url = child['link']['@href']
                self.__earthquake_intensity_report(url)
            elif title == '震源に関する情報':
                url = child['link']['@href']
                self.__epicenter_information(url)
            elif title == '震源・震度に関する情報':
                url = child['link']['@href']
                self.__information_on_epicenter_and_seismic_intensity(url)
            elif title == '緊急地震速報（予報）':
                url = child['link']['@href']
                self.__earthquake_early_warning_forecast(url)
            elif title == '緊急地震速報（警報）':
                url = child['link']['@href']
                self.__earthquake_early_warning_alarm(url)
            elif tsunami:
                url = child['link']['@href']
                self.__tsunami(url)

    def find_latest(self):
        '''
        最新の情報を振り分ける。
        また、同じ震源の地震情報が複数投稿された場合に「第何報」をつける。
        '''
        self.post_message = []
        earthquake_info_path = os.path.join(self.directory, 'latest_earthquake_info.json')
        earthquake_information = self.__load_buffer(earthquake_info_path, [])

        latest_information = earthquake_information
        self.formated_text.reverse()
        for individual in self.formated_text:
            if individual['data'] not in earthquake_information:
                if individual['title'] == '震度速報' or individual['title'] == '震源・震度に関する情報':
                    report_num = format_report(self.directory, individual['body'])
                    if report_num > 1:
                        individual['title'] += f'\n第{report_num}報'

                self.post_message.append(individual)

                latest_information.append(individual['data'])

        self.__save_buffer(earthquake_info_path, latest_information)

    def post_line(self):
        '''
        LINEにpostする。
        '''
        line_bot_api = linebot.LineBotApi(self.token)
        for message in self.post_message:
            flex_message = FlexSendMessage(
                alt_text=message["body"],
                contents=apply_template(message))
            line_bot_api.broadcast(flex_message)
        stop(self.post_message, line_bot_api)

    def __earthquake_intensity_report(self, url):
        '''
        フォーマット。
        -----
        震度速報
        [ここにメイン文]

        震度4: エリア1
        震度3: エリア2, エリア3, エリア4

        [注釈: str]
        -----
        > 震度速報
        '''
        text = {}
        try:
            earthquake_details = self.__request_text(url)
            details_root = xmltodict.parse(earthquake_details)

            text['title'] = '震度速報'
            text['body'] = details_root['Report']['Head']['Headline']['Text']

            area_info = self.__format_area(details_root)

            area_text = []
            for element in area_info:
                area_text.append(f'[{element}] {area_info[element]}')
            text['areas'] = area_text
            text['info'] = details_root['Report']['Body']['Comments']['ForecastComment']['Text']
            text['data'] = {
                'event_id': details_root['Report']['Head']['EventID'],
                'url': url
            }
        except xmltodict.expat.ExpatError:
            text['title'] = '震度速報'
            text['body'] = 'No data.'
            text['areas'] = ['[N/A] No data.']
            text['info'] = '今後の情報に注意してください。'
            text['data'] = {
                'event_id': 0000,
                'serial': 1,
                'url': url
            }

        self.formated_text.append(text)

    def __epicenter_information(self, url):
        '''
        フォーマット
        -----
        【震源に関する情報】
        [ここにメイン文]

        震源地: [area: str]
        マグニチュード: [マグニチュード: str]

        [注釈: str]
        -----
        > 震源に関する情報
        '''
        text = {}
        earthquake_details = self.__request_text(url)
        details_root = xmltodict.parse(earthquake_details)

        text['title'] = '震源に関する情報'
        text['body'] = details_root['Report']['Head']['Headline']['Text']
        text['magnitude'] = details_root['Report']['Body']['Earthquake']['jmx_eb:Magnitude']['#text']
        text['area'] = details_root['Report']['Body']['Earthquake']['Hypocenter']['Area']['Name']
        text['info'] = details_root['Report']['Body']['Comments']['ForecastComment']['Text']
        text['data'] = {
            'event_id': details_root['Report']['Head']['EventID'],
            'url': url
        }
        self.formated_text.append(text)

    def __information_on_epicenter_and_seismic_intensity(self, url):
        '''
        フォーマット
        -----
        【震源・震度に関する情報】
        [ここにメイン文]

        震源地: [エリア: str]

        マグニチュード: [マグニチュード: str]

        最大震度: [最大震度: str]

        [注釈: str]
        -----
        > 震源・震度に関する情報 (震度3以上のみ)
        '''
        text = {}
        earthquake_details = self.__request_text(url)
        details_root = xmltodict.parse(earthquake_details)

        text['title'] = '震源・震度に関する情報'
        text['body'] = details_root['Report']['Head']['Headline']['Text']
        text['magnitude'] = details_root['Report']['Body']['Earthquake']['jmx_eb:Magnitude']['#text']
        text['area'] = details_root['Report']['Body']['Earthquake']['Hypocenter']['Area']['Name']
        text['max_seismic_intensity'] = str(details_root['Report']['Body']['Intensity']['Observation']['MaxInt'])
        text['info'] = details_root['Report']['Body']['Comments']['ForecastComment']['Text']
        text['data'] = {
            'event_id': details_root['Report']['Head']['EventID'],
            'url': url
        }
        if 'Information' in details_root['Report']['Head']['Headline']:
            area_info = self.__format_area_detailed(details_root)
            area_text = []
            for element in area_info:
                area_text.append(f'[{element}] {area_info[element]}')
            text['areas'] = area_text

        if text['max_seismic_intensity'] in ['3', '4', '5-', '5+', '6-', '6+', '7']:
            self.formated_text.append(text)

    def __earthquake_early_warning_forecast(self, url):
        '''
        フォーマット
        -----
        【緊急地震速報 (予報)】
        [ここにメイン文]
        -----
        > 緊急地震速報（予報）
        '''
        text = {}
        earthquake_details = self.__request_text(url)
        details_root = xmltodict.parse(earthquake_details)

        text['title'] = '緊急地震速報(予報)'
        text['body'] = details_root['Report']['Head']['Headline']['Text']
        text['data'] = {
            'event_id': details_root['Report']['Head']['EventID'],
            'url': url
        }

        self.formated_text.append(text)

    def __earthquake_early_warning_alarm(self, url):
        '''
        フォーマット
        -----
        【緊急地震速報 (警報)】
        [ここにメイン文]

        エリア: エリア1, エリア2
        -----
        > 緊急地震速報（警報）
        '''
        text = {}
        earthquake_details = self.__request_text(url)
        details_root = xmltodict.parse(earthquake_details)

        text['title'] = '緊急地震速報 (警報)'
        text['body'] = details_root['Report']['Head']['Headline']['Text']
        text['data'] = {
            'event_id': details_root['Report']['Head']['EventID'],
            'url': url
        }

        area_info = self.__format_area(details_root)
        area_text = []
        for element in area_info:
            area_text.append(area_info[element])
        text['areas'] = area_text

        self.formated_text.append(text)

    def __tsunami(self, url):
        '''
        フォーマット
        -----
        【[タイトル文(大津波警報・津波警報・津波注意報・津波予報のうちどれかまたは全て)]】
        [ここにメイン文]

        エリア: エリア1, エリア2
        -----
        エリアは津波予報では表示されない
        > 津波関係すべて
        '''
        text = {}
        earthquake_details = self.__request_text(url)
        details_root = xmltodict.parse(earthquake_details)

        text['title'] = details_root['Report']['Head']['Title']
        text['body'] = details_root['Report']['Head']['Headline']['Text']
        text['data'] = {
            'event_id': details_root['Report']['Head']['EventID'],
            'url': url
        }

        if 'Information' in details_root['Report']['Head']['Headline']:
            area_info = self.__format_area(details_root)
            text['area'] = area_info[0]

        self.formated_text.append(text)

    def __format_area(self, details: Any) -> Dict[str, str]:
        '''
        震度とエリアの情報をフォーマットします。

        Args:
            details (Any): 元データ

        Returns:
            Dict[str, str]: フォーマットされたデータ。例: {'震度4': 'エリア1', '震度3': 'エリア2, エリア3, エリア4'}
        '''
        informations = details['Report']['Head']['Headline']['Information']
        if isinstance(informations, list):
            information = informations[0]['Item']
        else:
            information = informations['Item']

        return self.__select_area(information)

    def __format_area_detailed(self, details: Any) -> Dict[str, str]:
        '''
        震度とエリアの情報をフォーマットします。震源・震度に関する情報用

        Args:
            details (Any): 元データ

        Returns:
            Dict[str, str]: フォーマットされたデータ。例: {'震度4': 'エリア1', '震度3': 'エリア2, エリア3, エリア4'}
        '''
        informations = details['Report']['Head']['Headline']['Information']
        if isinstance(informations, list):
            for element in informations:
                if element['@type'] == '震源・震度に関する情報（市町村等）':
                    infomation = element['Item']
                    break
            else:
                return {'Null': 'No data.'}
        else:
            infomation = element['Item']

        return self.__select_area(infomation)

    @staticmethod
    def __select_area(information: Any) -> Dict[str, str]:
        '''
        エリアを取得します。

        Args:
            infomation[Any]: [{'Kind': {'Name': '震度'}, 'Areas': {'Area': [{'Name': '場所', 'Code': ''}]}}]
        Returns:
            Dict[str, str]: {'震度': 'エリア1、エリア2'}
        '''
        area_info = {}
        if isinstance(information, list):
            for individual in information:
                seismic_intensity = individual['Kind']['Name']
                areas = []
                if isinstance(individual['Areas']['Area'], list):
                    for area in individual['Areas']['Area']:
                        areas.append(area['Name'])
                else:
                    areas.append(individual['Areas']['Area']['Name'])
                area_info[seismic_intensity] = '、'.join(areas)
        else:
            seismic_intensity = information['Kind']['Name']
            areas = []
            if isinstance(information['Areas']['Area'], list):
                for area in information['Areas']['Area']:
                    areas.append(area['Name'])
            else:
                areas.append(information['Areas']['Area']['Name'])
            area_info[seismic_intensity] = '、'.join(areas)

        return area_info

    @staticmethod
    def __request_text(url: str) -> Any:
        '''
        リンクの内容を返します。

        Args:
            url (str): URL

        Returns:
            Any: 内容
        '''
        responce = requests.get(url)
        responce.encoding = 'UTF-8'
        return responce.text

    @staticmethod
    def __load_buffer(path: str, empty_element: Any) -> Any:
        '''
        バッファファイルを読み込む。
        もし、新規でファイルを作成する場合はフォーマットを任意に決定します。

        Args:
            path (str): 読み込むファイルのパス
            empty_element (Any): ファイルを新規作成するときに読み込む内容。

        Returns:
            Any: バッファの内容。新規作成した場合は`empty_element`がそのまま返される。
        '''
        if os.path.isfile(path):
            buffer = json_read(path)
        else:
            buffer = empty_element

        return buffer

    @staticmethod
    def __save_buffer(path: str, element: Any):
        '''
        バッファファイルを保存します。

        Args:
            path (str): 保存するファイルのパス
            element (Any): 保存する内容
        '''
        json_write(path, element)


if __name__ == "__main__":
    main()  # pylint: disable=E1120
