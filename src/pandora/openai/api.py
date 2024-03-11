# -*- coding: utf-8 -*-

import asyncio
import json
import queue as block_queue
import threading
from requests.models import Response

# import httpx
# import requests
from curl_cffi import requests
from certifi import where

from .. import __version__
from ..exts.config import default_api_prefix
from .utils import Console
import logging
from ..exts.hooks import hook_logging
from ..exts.config import USER_CONFIG_DIR

from os import getenv
import os
import json
from datetime import datetime
from dateutil.tz import tzutc
import uuid
import time
from bs4 import BeautifulSoup   # func get_origin_share_data

if os.path.exists(USER_CONFIG_DIR + '/api.json') and not getenv('PANDORA_OAI_ONLY'):
    from ..api.module import LocalConversation
    from ..api.module import API_CONFIG_FILE, API_DATA


class API:
    def __init__(self, proxy, ca_bundle):
        # self.proxy = proxy    # httpx
        self.proxy = {
                        'http': proxy,
                        'https': proxy,
                    }if proxy else None
        self.ca_bundle = ca_bundle
        self.web_origin = ''
        self.LOCAL_OP = getenv('PANDORA_LOCAL_OPTION')
        self.OAI_ONLY = getenv('PANDORA_OAI_ONLY')
        # self.conv = LocalConversation()

        # curl_cffi
        if 'nt' == os.name:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

                    
    @staticmethod
    def wrap_stream_out(generator, status):
        if status != 200:
            for line in generator:
                data = json.dumps(line)
                Console.debug_b('wrap_stream_out => statue != 200:{}'.format('data: ' + data + '\n\n'))

                msg_id = str(uuid.uuid4())
                create_time = int(time.time())
                fake_json = {"message": {"id": msg_id, "author": {"role": "assistant", "name": None, "metadata": {}}, "create_time": create_time, "update_time": None, "content": {"content_type": "text", "parts": [data]}, "status": "in_progress", "end_turn": None, "weight": 1.0, "metadata": {"citations": [], "gizmo_id": None, "message_type": "next", "parent_id": ""}, "recipient": "all"}, "error": data}
                
                # 尝试返回错误信息               
                yield b'data: ' + json.dumps(fake_json).encode('utf-8') + b'\n\n'
                yield b'data: [DONE]\n\n'

                # yield json.dumps(line)

            return

        for line in generator:
            yield b'data: ' + json.dumps(line).encode('utf-8') + b'\n\n'

        yield b'data: [DONE]\n\n'


    async def __process_sse(self, resp, conversation_id=None, message_id=None, model=None, action=None, prompt=None):
        if resp.status_code != 200:
            yield await self.__process_sse_except(resp)
            return
        
        BLOB_FLAGE = False
        headers_data = dict(resp.headers)
        # Console.debug_b('resp_headers_data: {}'.format(headers_data))
        if headers_data['content-type'].startswith('image/'):    # gan, cf返回的键名是小写
            BLOB_FLAGE = True
            img_type = headers_data['content-type'].split('/')[1]

        # 保证Headers: 'Content-Type':'text/event-stream;charset=UTF-8'
        # 否则如果直接透传，当某些API的响应头部'Content-Type'为json时，前端无法解析为SSE
        headers_data['Content-Type'] = 'text/event-stream;charset=UTF-8'
        headers_data['Transfer-Encoding'] = 'chunked'

        yield resp.status_code
        # yield resp.headers
        yield headers_data

        # Console.debug_b('__process_sse:status_code: {}'.format(resp.status_code))
        # Console.debug_b('__process_sse:headers: {}'.format(resp.headers))

        resp_content = ''
        yield_msg = ''
        create_time = None
        msg_id = None
        index = 0
        # SAVE_ASSISTANT_MSG = False

        msg_id = str(uuid.uuid4())
        create_time = int(time.time())
        
        if conversation_id is None and model in API_DATA:
            # Console.debug_b('No conversation_id, create and save user conversation.')
            conversation_id = str(uuid.uuid4())

            LocalConversation.create_conversation(conversation_id, prompt, datetime.now(tzutc()).isoformat())
            LocalConversation.save_conversation(conversation_id, message_id, prompt, 'user', datetime.now(tzutc()).isoformat(), model, action)
        else:
            # Console.debug_b('Exist conversation_id, save user conversation.')
            LocalConversation.save_conversation(conversation_id, message_id, prompt, 'user', datetime.now(tzutc()).isoformat(), model, action)

        if not BLOB_FLAGE:
            async for utf8_line in resp.aiter_lines():
                if isinstance(utf8_line, bytes):
                    utf8_line = utf8_line.decode('utf-8')
                
                # Console.debug_b('{}'.format(utf8_line))

                # 适配Real-Coze-API
                if '{"content"' == utf8_line[0:10] or b'{"content"' == utf8_line[0:10]:
                    for i in utf8_line.split('}'):
                        if i:
                            stream_data = json.loads(i + '}')
                            resp_content = stream_data['content']
                            # Console.debug_h('Coze => {}'.format(resp_content))
                            
                # 适配智谱CogView
                if '{"created"' == utf8_line[0:10] and 'cogview' in model:
                    resp_data = json.loads(utf8_line)
                    # resp_content = resp_data['url']
                    resp_content = '![img]({})'.format(resp_data['data'][0]['url'])

                if 'data: [DONE]' == utf8_line[0:12] or 'data: [DONE]' == utf8_line:
                    # if SAVE_ASSISTANT_MSG == False:
                    #     Console.debug_b('End of assistant's answer, save assistant conversation.')
                    #     LocalConversation.save_conversation(conversation_id, id, resp_content, 'assistant', datetime.now(tzutc()).isoformat(), model, action)
                        
                    # break
                    continue

                if 'data: {"message":' == utf8_line[0:17] or 'data: {"id":' == utf8_line[0:12]:
                    json_data = json.loads(utf8_line[6:])

                    if json_data.get('choices'):
                        create_time = json_data['created']
                        msg_id = json_data['id']

                        if json_data['choices'][0].get('message'):
                            resp_content = json_data['choices'][0]['message']['content']

                        elif json_data['choices'][0].get('delta'):  # 适配GLM
                            # print('{}'.format(json_data['choices'][0]['delta']['content']), end='')
                            try:
                                resp_content += json_data['choices'][0]['delta']['content']

                            except KeyError:
                                continue

                # 适配Gemini
                if '"text": ' == utf8_line[12:20] and 'gemini' in model:
                    text_json = json.loads('{' + utf8_line[12:] + '}')
                    resp_content += text_json['text']

                # 适配cloudflare ai
                if 'data: {"response":' == utf8_line[0:18]:
                    json_data = json.loads(utf8_line[6:])
                    resp_content += json_data['response']

                # 适配Double
                if 'double' in model:
                    resp_content += utf8_line

                if resp_content:
                    for char in resp_content[index:]:
                        yield_msg += char
                        fake_json = {"message": {"id": msg_id, "author": {"role": "assistant", "name": None, "metadata": {}}, "create_time": create_time, "update_time": None, "content": {"content_type": "text", "parts": [yield_msg]}, "status": "in_progress", "end_turn": None, "weight": 1.0, "metadata": {"citations": [], "gizmo_id": None, "message_type": "next", "model_slug": model, "parent_id": ""}, "recipient": "all"}, "conversation_id": conversation_id, "error": None}
                        index += 1

                        yield fake_json

        else:
            resp_content = await LocalConversation.save_image_file(resp, self.web_origin, msg_id, img_type)

            fake_json = {"message": {"id": msg_id, "author": {"role": "assistant", "name": None, "metadata": {}}, "create_time": create_time, "update_time": None, "content": {"content_type": "text", "parts": [resp_content]}, "status": "in_progress", "end_turn": None, "weight": 1.0, "metadata": {"citations": [], "gizmo_id": None, "message_type": "next", "model_slug": model, "parent_id": ""}, "recipient": "all"}, "conversation_id": conversation_id, "error": None}

            yield fake_json


        # Console.debug_b("End of assistant's answer, save assistant conversation.")
        LocalConversation.save_conversation(conversation_id, msg_id, resp_content, 'assistant', datetime.now(tzutc()).isoformat(), model, action)
  
    async def __process_sse_origin(self, resp):
        yield resp.status_code
        yield resp.headers

        if resp.status_code != 200:
            yield await self.__process_sse_except(resp)
            return

        async for utf8_line in resp.aiter_lines():
            if 'data: [DONE]' == utf8_line[0:12]:
                break

            if 'data: {"message":' == utf8_line[0:17] or 'data: {"id":' == utf8_line[0:12]:
                yield json.loads(utf8_line[6:])

    @staticmethod
    async def __process_sse_except(resp):
        result = b''
        # async for line in resp.aiter_bytes(): # httpx
        async for line in resp.aiter_lines():
            result += line

        return json.loads(result.decode('utf-8'))

    @staticmethod
    def __generate_wrap(queue, thread, event):
        while True:
            try:
                item = queue.get()
                if item is None:
                    break

                yield item
            except BaseException as e:
                event.set()
                thread.join()

                if isinstance(e, GeneratorExit):
                    raise e

    # async def _do_request_sse_httpx(self, url, headers, data, queue, event, conversation_id=None, message_id=None, model=None, action=None, prompt=None):
    #     try: 
    #         proxy = API_DATA[model].get('proxy')
    #     except KeyError:
    #         proxy = None

    #     async with httpx.AsyncClient(verify=self.ca_bundle, proxies=proxy if proxy else self.proxy) as client:
    #         async with client.stream('POST', url, json=data, headers=headers, timeout=600) as resp:
    #             async for line in self.__process_sse(resp, conversation_id, message_id, model, action, prompt):
    #                 queue.put(line)

    #                 if event.is_set():
    #                     await client.aclose()
    #                     break

    #             queue.put(None)

    async def _do_request_sse(self, url, headers, data, queue, event, conversation_id=None, message_id=None, model=None, action=None, prompt=None):
        proxy_url = API_DATA[model].get('proxy')
        proxy = {
                    "http": proxy_url,
                    "https": proxy_url,
                }if proxy_url else None

        async with requests.AsyncSession(verify=self.ca_bundle, proxies=proxy if proxy else self.proxy, impersonate='chrome110') as client:
            async with client.stream('POST', url, json=data, headers=headers, timeout=60) as resp:
                async for line in self.__process_sse(resp, conversation_id, message_id, model, action, prompt):
                    queue.put(line)

                    if event.is_set():
                        # await client.aclose()     # httpx
                        await client.close()
                        break

                queue.put(None)

    def _request_sse(self, url, headers, data, conversation_id=None, message_id=None, model=None, action=None, prompt=None):
        queue, e = block_queue.Queue(), threading.Event()
        t = threading.Thread(target=asyncio.run, args=(self._do_request_sse(url, headers, data, queue, e, conversation_id, message_id, model, action, prompt),))
        t.start()

        return queue.get(), queue.get(), self.__generate_wrap(queue, t, e)



class ChatGPT(API):
    def __init__(self, access_tokens: dict, proxy=None):
        self.access_tokens = access_tokens
        self.access_token_key_list = list(access_tokens)
        self.default_token_key = self.access_token_key_list[0]
        self.session = requests.Session()
        self.req_kwargs = {
            'proxies': {
                'http': proxy,
                'https': proxy,
            } if proxy else None,
            'verify': where(),
            'timeout': 100,
            'allow_redirects': False,
            'impersonate': 'chrome110',
        }

        # self.user_agent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) ' \
        #                   'Pandora/{} Safari/537.36'.format(__version__)
        self.user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.0.0'

        self.log_level = logging.INFO
        hook_logging(level=self.log_level, format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s')
        self.logger = logging.getLogger('waitress')

        super().__init__(proxy, self.req_kwargs['verify'])

    def __get_headers(self, token_key=None):
        return {
            'Authorization': 'Bearer ' + self.get_access_token(token_key),
            'User-Agent': self.user_agent,
            'Content-Type': 'application/json',
        }

    @staticmethod
    def __get_api_prefix():
        return getenv('OPENAI_API_PREFIX', default_api_prefix())
    
    def __get_api_req_kwargs(self, model):
        req_kwargs = self.req_kwargs

        if API_DATA[model].get('proxy'):
            req_kwargs['proxies'] = {
                'http': API_DATA[model]['proxy'],
                'https': API_DATA[model]['proxy'],
            }

        return req_kwargs

    def fake_resp(self, origin_resp=None, fake_data=None):
        fake_resp = Response()
        # Console.debug_b('fake_data: {}'.format(fake_data))
        fake_resp._content = fake_data.encode('utf-8')

        if origin_resp:
            fake_resp.headers = origin_resp.headers
            fake_resp.status_code = origin_resp.status_code
        else:
            fake_resp.headers = {'Content-Type': 'application/json'}
            fake_resp.status_code = 200

        fake_resp.encoding = 'utf-8'

        # Console.debug_b('fake_resp: ')
        # print(fake_resp.json())

        return fake_resp

    def get_access_token(self, token_key=None):
        return self.access_tokens[token_key or self.default_token_key]
    
    def double_generate_token(self, model, double_api_key):
        url = 'https://api.double.bot/api/auth/refresh'
        headers = {'Authorization': 'Bearer ' + double_api_key, 'User-Agent': self.user_agent}

        resp = self.session.post(url=url, headers=headers, **self.__get_api_req_kwargs(model))

        if resp.status_code == 200:
            access_token = resp.json().get('access_token')

            return access_token
        
        else:
            raise Exception('Double generate token failed: ' + self.__get_error(resp.text))

    def list_token_keys(self):
        return self.access_token_key_list
    
    def list_models(self, raw=False, token=None, web_origin=None):
        ERROR_FLAG = False
        self.web_origin = web_origin
        # Console.debug_b('web_origin: {}'.format(self.web_origin))

        if not self.LOCAL_OP:
            # url = '{}/api/models'.format(self.__get_api_prefix())
            url = '{}/backend-api/models'.format(self.__get_api_prefix())
            try:
                resp = self.session.get(url=url, headers=self.__get_headers(token), **self.req_kwargs)
            except:
                ERROR_FLAG = True

            if resp.status_code == 200:
                result = resp.json()

                if self.OAI_ONLY:
                    return self.fake_resp(fake_data=json.dumps(result, ensure_ascii=False))
                
        ## 动态更新models
        # models = result['models']
        # api_file = 'api.json'
        # if os.path.exists(API_CONFIG_FILE):
        #     with open(api_file, 'r', encoding='utf-8') as f:
        #         API_DATA = json.load(f)
        #     # print('====================需要加入的API====================')
        #     # print(API_DATA)
        #     # print('========================================')
        
        if API_DATA:
            gpt35_model = getenv('PANDORA_GPT35_MODEL')
            gpt4_model = getenv('PANDORA_GPT4_MODEL')
            gpt4_category = {
                            "category": "gpt_4",
                            "human_category_name": "GPT-4",
                            "subscription_level": "free",
                            "default_model": gpt4_model if gpt4_model else "gpt-4",
                            "plugins_model": gpt4_model if gpt4_model else "gpt-4"
                        }
            
            if self.LOCAL_OP or ERROR_FLAG ==True or resp.status_code != 200:
                result = {
                    "models": [
                        {
                            "slug": "text-davinci-002-render-sha",
                            "max_tokens": 8191,
                            "title": "Default (GPT-3.5)",
                            "description": "Our fastest model, great for most everyday tasks.",
                            "tags": [
                                "gpt3.5"
                            ],
                            "capabilities": {},
                            "product_features": {}
                        }if not gpt35_model else None
                    ],
                    "categories": [
                        {
                            "category": "gpt_3.5",
                            "human_category_name": "GPT-3.5",
                            "subscription_level": "free",
                            "default_model": "text-davinci-002-render-sha",
                            "code_interpreter_model": "text-davinci-002-render-sha-code-interpreter",
                            "plugins_model": "text-davinci-002-render-sha-plugins"
                        }
                    ]
                }

            for item in API_DATA.values():
                title = item['title']
                slug = item['slug']
                description = item['description']
                max_tokens = item['max_tokens']

                model_json = {
                    "capabilities": {},
                    "description": description,
                    "enabled_tools": [
                        "tools",
                        "tools2"
                    ],
                    "max_tokens": max_tokens,
                    "product_features": {},
                    "slug": slug,
                    "tags": [
                        "gpt3.5"
                    ],
                    "title": title
                }

                result['models'].append(model_json)

            result['categories'].append(gpt4_category)

            # 此操作会导致不显示除3.5&4外的其他模型(Alpha Models), 已在server端重定向
            # if gpt35_model:
            #     for category in result['categories']:
            #         if category['category'] == 'gpt_3.5':
                        # category['default_model'] = gpt35_model
                        # category['plugins_model'] = gpt35_model
                        # category['code_interpreter_model'] = gpt35_model

            if self.LOCAL_OP or resp.status_code != 200:
                return self.fake_resp(fake_data=json.dumps(result, ensure_ascii=False))

            return self.fake_resp(resp, json.dumps(result, ensure_ascii=False))

        if raw:
            # print(resp.text)
            return resp

        if not self.LOCAL_OP and resp.status_code != 200:
            raise Exception('list models failed: ' + self.__get_error(resp))

        
        if 'models' not in result:
            raise Exception('list models failed: ' + resp.text)

    def list_conversations(self, offset, limit, raw=False, token=None):
        ERROR_FLAG = False
        if not self.LOCAL_OP:
            # url = '{}/api/conversations?offset={}&limit={}'.format(self.__get_api_prefix(), offset, limit)
            url = '{}/backend-api/conversations?offset={}&limit={}&order=updated'.format(self.__get_api_prefix(), offset, limit)
            try:
                resp = self.session.get(url=url, headers=self.__get_headers(token), **self.req_kwargs)
            except:
                ERROR_FLAG = True

            if resp.status_code == 200:
                result = resp.json()

                if self.OAI_ONLY:
                    return self.fake_resp(fake_data=json.dumps(result, ensure_ascii=False))

        if self.LOCAL_OP or ERROR_FLAG == True or resp.status_code != 200:
            result = {
                'has_missing_conversations': False,
                'items': [],
                'limit': limit,
                'offset': offset,
                'total': 0,
            }

        convs_data = LocalConversation.list_conversations(offset, limit)
        # Console.debug_b('Local conversation list: {}'.format(convs_data))
        if convs_data:
            convs_data_total = convs_data['total']

            if convs_data.get('list_data'):
                for item in convs_data['list_data']:
                    if item['visible'] == 1 or item['visible'] == '1':
                        id = item['id']
                        title = item['title']
                        create_time = item['create_time']
                        update_time = item['update_time']

                        final_item = {
                            "id": id,
                            "title": title,
                            "create_time": create_time,
                            "update_time": update_time,
                            "mapping": None,
                            "current_node": None,
                            "conversation_template_id": None,
                            "gizmo_id": None,
                            "is_archived": False,
                            "workspace_id": None
                        }

                        # final_item_json = json.dumps(final_item, ensure_ascii=False)
                        result['items'].append(final_item)

                if not self.LOCAL_OP and resp.status_code == 200:
                    # 对话列表按更新时间'update_time'倒序重新排序
                    result['items'] = sorted(result['items'], key=lambda item: item['update_time'], reverse=True)
                    result['total'] = convs_data_total if convs_data_total > result['total'] else result['total']

                    return self.fake_resp(resp, json.dumps(result, ensure_ascii=False))
                    # return self.fake_resp(resp, result)
                else:
                    # from datetime import timezone
                    # now = datetime.now(timezone.utc).isoformat()
                    ## 当获取oai对话列表失败时, 在对话列表中插入警告项
                    # warning_item = {
                    #         "id": "warning",
                    #         "title": "!!! Get oai convs list failed !!!",
                    #         "create_time": now,
                    #         "update_time": now,
                    #         "mapping": None,
                    #         "current_node": None,
                    #         "conversation_template_id": None,
                    #         "gizmo_id": None,
                    #         "is_archived": False,
                    #         "workspace_id": None
                    #     }
                    # result['items'].insert(0, warning_item)
                    result['items'] = sorted(result['items'], key=lambda item: item['update_time'], reverse=True)
                    result['total'] = convs_data_total

                    return self.fake_resp(fake_data=json.dumps(result, ensure_ascii=False))
                
        return self.fake_resp(fake_data=json.dumps(result, ensure_ascii=False))

    def register_websocket(self, request, token=None):
        if self.LOCAL_OP:
            return 404
        
        url = '{}/backend-api/register-websocket'.format(self.__get_api_prefix())
        data = request.data
        resp = self.session.post(url=url, headers=self.__get_headers(token), data=data, **self.req_kwargs)

        return resp
    
    def arkose_dx(self, request, token=None):
        url = '{}/backend-api/sentinel/arkose/dx'.format(self.__get_api_prefix())
        data = request.data
        resp = self.session.post(url=url, headers=self.__get_headers(token), data=data, **self.req_kwargs)

        return resp

    def get_conversation(self, conversation_id, raw=False, token=None):
        if os.path.exists(API_CONFIG_FILE) or not self.OAI_ONLY:
            conversation_info = LocalConversation.check_conversation_exist(conversation_id)

            if conversation_info:
                return LocalConversation.get_conversation(conversation_id)

        # url = '{}/api/conversation/{}'.format(self.__get_api_prefix(), conversation_id)
        url = '{}/backend-api/conversation/{}'.format(self.__get_api_prefix(), conversation_id)
        resp = self.session.get(url=url, headers=self.__get_headers(token), **self.req_kwargs)

        if raw:
            return resp

        if resp.status_code != 200:
            raise Exception('get conversation failed: ' + self.__get_error(resp))

        return resp.json()

    # 新ui已无清空对话功能
    def clear_conversations(self, raw=False, token=None):
        data = {
            'is_visible': False,
        }

        url = '{}/backend-api/conversations'.format(self.__get_api_prefix())
        resp = self.session.patch(url=url, headers=self.__get_headers(token), json=data, **self.req_kwargs)

        if raw:
            return resp

        if resp.status_code != 200:
            raise Exception('clear conversations failed: ' + self.__get_error(resp))

        result = resp.json()
        if 'success' not in result:
            raise Exception('clear conversations failed: ' + resp.text)

        return result['success']

    def del_conversation(self, conversation_id, raw=False, token=None):
        if os.path.exists(API_CONFIG_FILE):
            conversation_info = LocalConversation.check_conversation_exist(conversation_id)
            if conversation_info:
                return LocalConversation.del_conversation(conversation_id)
            
        data = {
            'is_visible': False,
        }

        return self.__update_conversation(conversation_id, data, raw, token)

    def gen_conversation_title(self, conversation_id, message_id, raw=False, token=None):
        if self.LOCAL_OP:
            return 404
        
        url = '{}/backend-api/conversation/gen_title/{}'.format(self.__get_api_prefix(), conversation_id)
        data = {
            'message_id': message_id,
        }
        resp = self.session.post(url=url, headers=self.__get_headers(token), json=data, **self.req_kwargs)

        if raw:
            return resp

        if resp.status_code != 200:
            raise Exception('gen title failed: ' + self.__get_error(resp))

        result = resp.json()

        if self.OAI_ONLY:
            return self.fake_resp(fake_data=json.dumps(result, ensure_ascii=False))

        if 'title' not in result:
            raise Exception('gen title failed: ' + resp.text)

        return result['title']

    def set_conversation_title(self, conversation_id, title, raw=False, token=None):
        if os.path.exists(API_CONFIG_FILE):
            conversation_info = LocalConversation.check_conversation_exist(conversation_id)

            if conversation_info:
                return LocalConversation.rename_conversation(title, conversation_id)
            
        data = {
            'title': title,
        }

        return self.__update_conversation(conversation_id, data, raw, token)
    
    def error_fallback(self, content):
        resp = Response()
        resp.headers = {'Content-Type': 'text/event-stream;charset=UTF-8'}
        resp.status_code = 200

        error_content = 'System Error: ' + content + ' !'
        msg_id = str(uuid.uuid4())
        create_time = int(time.time())
        fake_json = {"message": {"id": msg_id, "author": {"role": "assistant", "name": None, "metadata": {}}, "create_time": create_time, "update_time": None, "content": {"content_type": "text", "parts": [error_content]}, "status": "in_progress", "end_turn": None, "weight": 1.0, "metadata": {"citations": [], "gizmo_id": None, "message_type": "next", "parent_id": ""}, "recipient": "all"}, "error": error_content}

        resp_content = b'data: ' + json.dumps(fake_json).encode('utf-8') + b'\n\n' + b'data: [DONE]\n\n'
        resp._content = resp_content

        return resp

    def talk(self, prompt, model, message_id, parent_message_id, conversation_id=None, stream=True, token=None):
        data = {
            'action': 'next',
            'messages': [
                {
                    'id': message_id,
                    'role': 'user',
                    'author': {
                        'role': 'user',
                    },
                    'content': {
                        'content_type': 'text',
                        'parts': [prompt],
                    },
                }
            ],
            'model': model,
            'parent_message_id': parent_message_id,
        }

        if conversation_id:
            data['conversation_id'] = conversation_id

        return self.__request_conversation(data, token)
    

    def chat_ws(self, payload, token=None):
        if self.LOCAL_OP:
            return self.error_fallback('OAI not supported')

        url = '{}/backend-api/conversation'.format(self.__get_api_prefix())
        resp = self.session.post(url=url, headers=self.__get_headers(token), json=payload, **self.req_kwargs)

        return resp
    
    def get_text_gen_img_prompt(self, content, url, prompt_model, gen_img_model=None):
        auth = LocalConversation.get_auth(prompt_model)
        origin_prompt = API_DATA[gen_img_model].get('prompt')
        # Console.debug_b('prompt_model: {} | prompt_model_auth: {} | gen_img_model: {}'.format(prompt_model, auth, gen_img_model))
        
        if origin_prompt:
            prompt_content = origin_prompt.replace('<Prompt>', content)

            if origin_prompt == prompt_content:
                prompt_content += content

        else:
            prompt_content = content

        prompt_data = { "messages": [{ "role": "user", "content": prompt_content }]}
        headers = {'User-Agent': self.user_agent, 'Content-Type': 'application/json'}

        if not prompt_model.startswith('@cf'):
            prompt_data['model'] = prompt_model

            if 'glm' in prompt_model:
                auth = LocalConversation.glm_generate_token(auth, 3600)

            if 'double' in prompt_model:
                double_api_key = auth
                auth = self.double_generate_token(prompt_model, double_api_key)
                headers['double-version'] = '2024-03-04'

                prompt_data['api_key'] = double_api_key
                prompt_data['chat_model'] = 'GPT4 Turbo' if 'GPT' in prompt_model or 'gpt' in prompt_model else 'Claude 3 (Opus)'
                del prompt_data['model']

                for item in prompt_data['messages']:
                    if item.get('content'):
                        item['message'] = item['content']
                        del item['content']

                    if item['role'] == 'user':
                        item['codeContexts'] = []

            if 'gemini' in prompt_model:
                headers = {'User-Agent': self.user_agent, 'Content-Type': 'application/json'}
                prompt_data = {"contents":[]}

                if prompt:
                    prompt_data['contents'].append({"role": "system", "parts": [{"text": prompt}]})

                prompt_data['contents'].append({"role": "user", "parts": [{"text": prompt_content}]})

        if auth:
            headers['Authorization'] = 'Bearer ' + auth

        # Console.debug_b('get_text_gen_img_prompt=>url: {}'.format(url))
        # Console.debug_b('get_text_gen_img_prompt=>headers: {}'.format(headers))
        prompt_resp = self.session.post(url=url, headers=headers, json=prompt_data, **self.__get_api_req_kwargs(prompt_model))

        if prompt_resp.status_code == 200:
            prompt_data = prompt_resp.json()
            prompt = ''

            if prompt_data.get('result'):   # Cloudflare AI
                prompt = prompt_data['result']['response']
            elif prompt_data.get('choices'):
                prompt = prompt_data['choices'][0]['message']['content']

            # Console.debug_b('get_text_gen_img_prompt: {}'.format(prompt))

            return prompt
        else:
            Console.warn('get_text_gen_img_prompt failed: {}'.format(prompt_resp.text))
            return None
    
    # 已废弃
    def cfai_text_gen_img(self, payload, token=None):
        content = str(payload['messages'][0]['content']['parts'][0])
        model = payload['model']
        base_url = LocalConversation.get_url(model) if not LocalConversation.get_url(model).endswith('/') else LocalConversation.get_url(model)[:-1]
        img_url = base_url + '/' + API_DATA[model].get('image_model')
        auth = LocalConversation.get_auth(model)
        headers = {'Authorization': 'Bearer ' + auth, 'User-Agent': self.user_agent, 'Content-Type': 'application/json'}
        fake_data = {"prompt": content}

        if API_DATA[model].get('prompt_model'):
            prompt_url = base_url + '/' + API_DATA[model].get('prompt_model')
            # prompt_data = { "messages": [{ "role": "user", "content": "你是专业的ai prompt生成师，现在请你认真体悟文字的场景与氛围并生成关于'{}'的AI drawing prompt，言简意骇，请不要出现任何中文，如有中文则自动翻译至英语。最后直接输出prompt的主要内容即可".format(content) }]}
            prompt_data = { "messages": [{ "role": "user", "content": "You are a professional ai prompt generator, now please seriously realize the scene and atmosphere of the text and generate an AI drawing prompt about '{}', please don't show any Chinese, if there is any Chinese, it will be automatically translated to English. Finally, you can output the main content of the prompt directly.".format(content) }]}
            prompt_resp = self.session.post(url=prompt_url, headers=headers, json=prompt_data, **self.__get_api_req_kwargs(model))

            if prompt_resp.status_code == 200:
                prompt = prompt_resp.json()['result']['response']
                fake_data = {"prompt": prompt}

        resp = self.session.post(url=img_url, headers=headers, json=fake_data, **self.__get_api_req_kwargs(model))

        return resp

    def goon(self, model, parent_message_id, conversation_id, stream=True, token=None):
        data = {
            'action': 'continue',
            'conversation_id': conversation_id,
            'model': model,
            'parent_message_id': parent_message_id,
        }

        return self.__request_conversation(data, token)

    def regenerate_reply(self, prompt, model, conversation_id, message_id, parent_message_id, stream=True, token=None):
        data = {
            'action': 'variant',
            'messages': [
                {
                    'id': message_id,
                    'role': 'user',
                    'author': {
                        'role': 'user',
                    },
                    'content': {
                        'content_type': 'text',
                        'parts': [prompt],
                    },
                }
            ],
            'model': model,
            'conversation_id': conversation_id,
            'parent_message_id': parent_message_id,
        }

        return self.__request_conversation(data, token)
    

    def create_share(self, request, token=None):
        host = request.host_url
        payload = request.json
        conversation_id = payload['conversation_id']
        current_node_id = payload['current_node_id']
        is_anonymous = payload['is_anonymous']
        resp_data = {
                    "share_id": "",
                    "share_url": "",
                    "title": "",
                    "is_public": True,
                    "is_visible": True,
                    "is_anonymous": is_anonymous,
                    "highlighted_message_id": None,
                    "current_node_id": current_node_id,
                    "already_exists": True,
                    "moderation_state": {
                        "has_been_moderated": False,
                        "has_been_blocked": False,
                        "has_been_accepted": False,
                        "has_been_auto_blocked": False,
                        "has_been_auto_moderated": False
                    }
            }
        title = self.cursor.execute("SELECT title FROM list_conversations WHERE id=?", (conversation_id,)).fetchone()
        # Console.debug_b('create_share: {}'.format(title))
        if title:
            is_anonymous = payload['is_anonymous']
            current_node_id = payload['current_node_id']
            resp_data['share_id'] = conversation_id
            resp_data['share_url'] = host + "share/" + conversation_id
            resp_data['title'] = title[0]

            return self.fake_resp(fake_data=json.dumps(resp_data, ensure_ascii=False))
        
        if self.LOCAL_OP:
            return 404
        
        url = '{}/backend-api/share/create'.format(self.__get_api_prefix())
        resp = self.session.post(url=url, headers=self.__get_headers(token), json=payload, **self.req_kwargs)
        if resp.status_code == 200:
            resp_data = resp.json()
            share_url = host + "share/" + resp_data['share_id']
            resp_data['share_url'] = share_url

            return self.fake_resp(fake_data=json.dumps(resp_data, ensure_ascii=False))
        
        return resp
    
    def get_origin_share_data(self, share_id, token=None):
        url = '{}/share/{}'.format(self.__get_api_prefix(), share_id)
        resp = self.session.get(url=url, headers=self.__get_headers(token), **self.req_kwargs)
        if resp.status_code == 200:
            html = resp.text
            soup = BeautifulSoup(html, 'html.parser')
            scripts = soup.find_all('script')  # 找到所有的脚本
            for script in scripts:
                if script.get('id') == '__NEXT_DATA__':
                    json_text = script.string
                    origin_share_data = json.loads(json_text)
                    Console.debug_b('origin_share_data: {}'.format(origin_share_data))
                    serverResponse_data = origin_share_data['props']['pageProps']['serverResponse']
                    serverResponse_data['continue_conversation_url'] = serverResponse_data['continue_conversation_url'].split('https://chat.openai.com')[1]     # 去掉oai的host, 最后在server.py-get_share_page再添加host

                    return serverResponse_data
        else:
            raise Exception('get_origin_share_data failed: \n' + self.__get_error(resp) + '\n' + str(resp.status_code))

        # return resp
            

    def get_share_data(self, share_id, token=None):
        script_json = {'It has been deleted by GavinGoo.'}

        conv_share_data = LocalConversation.get_conv_share_data(share_id)
        if conv_share_data:
            script_json['props']['pageProps']['serverResponse'] = conv_share_data
            script_json['query']['shareParams'][0] = conv_share_data['data']['conversation_id']

        elif not self.LOCAL_OP:
            origin_conv_share_data = self.get_origin_share_data(share_id, token)
            # 待修改
            script_json['props']['pageProps']['serverResponse'] = origin_conv_share_data
            script_json['query']['shareParams'][0] = origin_conv_share_data['data']['conversation_id']

        return script_json
    
    


    def __request_conversation(self, data, token=None):
        if not getenv('PANDORA_LOCAL_OPTION'):
            # url = '{}/api/conversation'.format(self.__get_api_prefix())
            url = '{}/backend-api/conversation'.format(self.__get_api_prefix())
            headers = {**self.session.headers, **self.__get_headers(token), 'Accept': 'text/event-stream'}
        else:
            headers = {**self.session.headers, 'Accept': 'text/event-stream'}

        if data['model'] in API_DATA:
            content = str(data['messages'][0]['content']['parts'][0])
            model = data['model']
            prompt_model = API_DATA[model].get('prompt_model')
            prompt = API_DATA[model].get('prompt')
            message_id = data['messages'][0]['id']
            action = data['action']
            conversation_id = None
            url = LocalConversation.get_url(model)
            auth = LocalConversation.get_auth(model)
            headers = {'User-Agent': self.user_agent, 'Content-Type': 'application/json'}
            history_list = []
            fake_data = {
                "messages": [],
                "model": model,
                "stream": True,
            }
            # Console.warn('{} | {}'.format(model, auth))

            if 'glm' in model or 'cogview' in model:
                auth = LocalConversation.glm_generate_token(auth, 3600)
                # Console.debug_b('生成的GLM_Token: {}'.format(auth))

            if 'double' in model:
                double_api_key = auth
                auth = self.double_generate_token(model, double_api_key)
                # Console.debug_b('生成的Double_Token: {}'.format(auth))

            if auth:
                headers['Authorization'] = 'Bearer ' + auth

            if prompt and not prompt_model:
                if 'double' in model:
                    fake_data['messages'].append({"role": "user", "message": prompt})
                    fake_data['messages'].append({"role":"assistant","message":"Ok, I get it."})

                else:
                    fake_data['messages'].append({"role": "system", "content": prompt})

            # 调用其他模型优化生图Prompt
            if prompt and prompt_model:
                if prompt_model.startswith('@cf'):
                    prompt_url = base_url + '/' + prompt_model
                else:
                    prompt_url = LocalConversation.get_url(prompt_model)

                prompt = self.get_text_gen_img_prompt(content, prompt_url, model if prompt_model.startswith('@cf') else prompt_model, model)

                if prompt:
                    content = prompt

            if data.get('conversation_id'):
                conversation_id = data['conversation_id']
                history_list = LocalConversation.get_history_conversation(conversation_id)

                for item in history_list:
                    fake_data['messages'].append({"role": item['role'], "content": item['message']})

            # 适配Cloudflare AI: text_gen_img
            if model == 'stable-diffusion-xl-base-1.0' or model == 'dreamshaper-8-lcm' or model == 'stable-diffusion-xl-lightning':
                base_url = LocalConversation.get_url(model) if not LocalConversation.get_url(model).endswith('/') else LocalConversation.get_url(model)[:-1]
                img_url = base_url + '/' + API_DATA[model].get('image_model')
                gen_img_data = {"prompt": content}

                return self._request_sse(img_url, headers, gen_img_data, conversation_id, message_id, model, action, content)

            if 'gemini' in model:
                headers = {'User-Agent': self.user_agent, 'Content-Type': 'application/json'}
                fake_data = {"contents":[]}

                if prompt:
                    fake_data['contents'].append({"role": "system", "parts": [{"text": prompt}]})

                if history_list:
                    for item in history_list:
                        fake_data['contents'].append({"role": "user" if item['role'] == 'user' else "model", "parts": [{"text": item['message']}]})

                fake_data['contents'].append({"role": "user", "parts": [{"text": content}]})
                # Console.debug_b(fake_data)
                return self._request_sse(url, headers, fake_data, conversation_id, message_id, model, action, content)
            
            # # 适配coze-real-api   # R.I.P
            # if model == 'coze-cra' or model == 'coze-real-api':
            #     fake_data = []
            #     if history_list:
            #         for item in history_list:
            #             fake_data.append({"role": 2 if item['role'] == 'user' else 1, "content": item['message']})
            #     fake_data.append({"role": 2, "content": content})

            #     return self._request_sse(url, headers, fake_data, conversation_id, message_id, model, action, content)
            
            # 适配智谱AI文生图
            if 'cogview' in model:
                fake_data = {"model": "cogview-3", "prompt": content}

                return self._request_sse(url, headers, fake_data, conversation_id, message_id, model, action, content)

            fake_data['messages'].append({"role": "user", "content": content})

            # 适配Double(需重新处理请求体)
            if 'double' in model:
                headers['double-version'] = '2024-03-04'
                fake_data['api_key'] = double_api_key
                fake_data['chat_model'] = 'GPT4 Turbo' if 'GPT' in model or 'gpt' in model else 'Claude 3 (Opus)'
                del fake_data['model']

                for item in fake_data['messages']:
                    if item.get('content'):
                        item['message'] = item['content']
                        del item['content']

                    if item['role'] == 'user':
                        item['codeContexts'] = []   # user对话需要带上codeContexts, 否则报错

            return self._request_sse(url, headers, fake_data, conversation_id, message_id, model, action, content)

        return self._request_sse(url=url, headers=headers, data=data)

    def __update_conversation(self, conversation_id, data, raw=False, token=None):
        url = '{}/backend-api/conversation/{}'.format(self.__get_api_prefix(), conversation_id)
        # url = '{}/backend-api/conversation/{}'.format(self.__get_api_prefix(), conversation_id)
        resp = self.session.patch(url=url, headers=self.__get_headers(token), json=data, **self.req_kwargs)

        if raw:
            return resp

        if resp.status_code != 200:
            raise Exception('update conversation failed: ' + self.__get_error(resp))

        result = resp.json()
        if 'success' not in result:
            raise Exception('update conversation failed: ' + resp.text)

        return result['success']

    @staticmethod
    def __get_error(resp):
        try:
            return str(resp.json()['detail'])
        except:
            return resp.text


class ChatCompletion(API):
    def __init__(self, proxy=None):
        self.session = requests.Session()
        self.req_kwargs = {
            'proxies': {
                'http': proxy,
                'https': proxy,
            } if proxy else None,
            'verify': where(),
            'timeout': 600,
            'allow_redirects': False,
        }

        self.user_agent = 'pandora/{}'.format(__version__)

        super().__init__(proxy, self.req_kwargs['verify'])

    def __get_headers(self, api_key):
        return {
            'Authorization': 'Bearer ' + api_key,
            'User-Agent': self.user_agent,
            'Content-Type': 'application/json',
        }

    def request(self, api_key, model, messages, stream=True, **kwargs):
        data = {
            'model': model,
            'messages': messages,
            **kwargs,
            'stream': stream,
        }

        return self.__request_conversation(api_key, data, stream)

    def __request_conversation(self, api_key, data, stream):
        default = default_api_prefix()

        if api_key.startswith('fk-') or api_key.startswith('pk-'):
            prefix = default
        else:
            prefix = getenv('OPENAI_API_PREFIX', default)
        url = '{}/v1/chat/completions'.format(prefix)

        if stream:
            headers = {**self.__get_headers(api_key), 'Accept': 'text/event-stream'}
            return self._request_sse(url=url, headers=headers, data=data)

        resp = self.session.post(url=url, headers=self.__get_headers(api_key), json=data, **self.req_kwargs)

        def __generate_wrap():
            yield resp.json()

        return resp.status_code, resp.headers, __generate_wrap()
