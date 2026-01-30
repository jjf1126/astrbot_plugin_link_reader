import re
import asyncio
import traceback
import base64
import json
from typing import Optional, List, Dict
from urllib.parse import urlparse, quote

import aiohttp
from bs4 import BeautifulSoup

# 尝试导入 Playwright 截图组件
try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "自动解析链接内容，支持社交平台截图及 geciyi.com 定向歌词搜索。", "1.3.1")
class LinkReaderPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 加载基础配置
        self.general_config = self.config.get("general_config", {})
        self.enable_plugin = self.general_config.get("enable_plugin", True)
        self.max_length = self.general_config.get("max_content_length", 2000)
        self.timeout = self.general_config.get("request_timeout", 15)
        self.user_agent = self.general_config.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        self.prompt_template = self.general_config.get("prompt_template", "\n【以下是链接的具体内容，请参考该内容进行回答】：\n{content}\n")

        # 加载平台 Cookie
        self.platform_cookies = self.config.get("platform_cookies", {})

        # URL 匹配正则
        self.url_pattern = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[/\w\.-]*\??[\w=&%\.-]*')

    def _get_headers(self, domain: str = "") -> dict:
        """根据域名获取对应的 Headers (包含 Cookie)"""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
        }
        cookie_key = None
        if "xiaohongshu" in domain: cookie_key = "xiaohongshu"
        elif "zhihu" in domain: cookie_key = "zhihu"
        elif "weibo" in domain: cookie_key = "weibo"
        elif "bilibili" in domain: cookie_key = "bilibili"
        elif "douyin" in domain: cookie_key = "douyin"
        elif "tieba.baidu" in domain: cookie_key = "tieba"
        elif "lofter" in domain: cookie_key = "lofter"

        if cookie_key:
            cookie_val = self.platform_cookies.get(cookie_key, "")
            if cookie_val:
                headers["Cookie"] = cookie_val
        return headers

    def _is_music_site(self, url: str) -> bool:
        """判断是否为音乐网站 (包含短链接)"""
        music_domains = ["music.163.com", "y.qq.com", "kugou.com", "kuwo.cn", "163cn.tv", "url.cn", "163.fm"]
        return any(domain in url for domain in music_domains)

    def _contains_chinese(self, text: str) -> bool:
        """检测文本是否包含汉字"""
        for char in text:
            if '\u4e00' <= char <= '\u9fff':
                return True
        return False

    def _filter_lyrics(self, lyrics: str) -> str:
        """参考 lyricnext 的深度清洗逻辑，去除元数据和时间轴"""
        lines = lyrics.split('\n')
        filtered_lines = []
        for line in lines:
            line = line.strip()
            if not line: continue
            # 去除时间标签
            line = re.sub(r'\[\d+:\d+\.\d+\]', '', line).strip()
            if not line or line.startswith('['): continue
            
            # 过滤掉信息行（作词、作曲、标题等）
            if (':' in line or '：' in line or ' - ' in line or 
                ('(' in line and ')' in line) or re.match(r'^[A-Za-z\s:]+$', line)):
                continue
            
            # 汉字歌词空格拆分逻辑
            if ' ' in line and self._contains_chinese(line):
                parts = [part.strip() for part in line.split(' ') if part.strip()]
                if all(len(part) < 20 and not any(c in part for c in ':：()[]{}') for part in parts):
                    filtered_lines.extend(parts)
                    continue
            
            filtered_lines.append(line)
        
        # 过滤数字行和短行
        final_lines = [l for l in filtered_lines if len(l) > 1 and not l.isdigit()]
        return '\n'.join(final_lines)

    def _clean_text(self, text: str) -> str:
        """核心清洗逻辑：剔除无关的备案信息、页脚、法律条款等默认内容"""
        lines = text.split('\n')
        # 无关信息黑名单
        blacklist = [
            "沪ICP备", "公网安备", "经营许可证", "版权所有", "©", "Copyright", 
            "地址：", "电话：", "商务合作", "违法不良信息", "加载中", "立即体验",
            "下载APP", "打开APP", "营业执照", "医疗器械", "网信算备"
        ]
        
        cleaned_lines = []
        for line in lines:
            line = line.strip()
            # 跳过空行、过短的行或包含黑名单内容的行
            if not line or len(line) < 2 or any(kw in line for kw in blacklist):
                continue
            cleaned_lines.append(line)
            
        result = '\n'.join(cleaned_lines)
        result = re.sub(r' +', ' ', result).strip()
        
        if len(result) > self.max_length:
            result = result[:self.max_length] + "...(内容过长已截断)"
        return result

    async def _search_geciyi(self, song_name: str) -> Optional[str]:
        """直接在 geciyi.com 搜索并点击第一个“查看歌词”选项"""
        search_url = f"https://geciyi.com/zh-Hans/search?q={quote(song_name)}"
        headers = {"User-Agent": self.user_agent}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, headers=headers, timeout=10) as resp:
                    if resp.status != 200: return None
                    html = await resp.text()
                    soup = BeautifulSoup(html, 'lxml')
                    
                    # 寻找搜索结果中的第一个“查看歌词”链接
                    target_link = None
                    for a in soup.find_all('a', href=True):
                        if "查看歌詞" in a.get_text():
                            target_link = a['href']
                            if not target_link.startswith("http"):
                                target_link = "https://geciyi.com" + target_link
                            break
                    
                    if target_link:
                        logger.info(f"[LinkReader] 正在访问歌词详情页: {target_link}")
                        async with session.get(target_link, headers=headers, timeout=10) as l_resp:
                            l_soup = BeautifulSoup(await l_resp.text(), 'lxml')
                            # 尝试定位歌词主容器
                            content_container = l_soup.find('article') or l_soup.find('div', class_='lyric-content')
                            
                            if content_container:
                                for tag in content_container(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'button']):
                                    tag.decompose()
                                raw_text = content_container.get_text(separator='\n', strip=True)
                            else:
                                for tag in l_soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'button']):
                                    tag.decompose()
                                raw_text = l_soup.get_text(separator='\n', strip=True)
                                
                            # 使用深度过滤逻辑处理歌词
                            return self._filter_lyrics(raw_text)
        except Exception as e:
            logger.error(f"[LinkReader] geciyi.com 搜索失败: {e}")
        return None

    async def _handle_music_smart_search(self, url: str) -> str:
        """音乐解析入口：提取曲名并定向搜索"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": self.user_agent}, timeout=5) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    title = soup.title.string.strip() if soup.title else url
            
            song_name = re.sub(r'( - 网易云音乐| - QQ音乐| - 酷狗音乐| - 酷我音乐|\|.*)$', '', title).strip()
            song_name = re.sub(r' - .*$', '', song_name).strip() 
            
            logger.info(f"[LinkReader] 正在定向搜索歌词: {song_name}")
            content = await self._search_geciyi(song_name)
            
            if content:
                return f"【歌词解析: {song_name}】\n数据来源: geciyi.com\n\n{content}"
            return f"识别到歌曲《{song_name}》，但未能从 geciyi.com 获取正确歌词。"
        except Exception as e:
            return f"音乐解析异常: {str(e)}"

    async def _get_screenshot_and_content(self, url: str):
        """Playwright 浏览器自动化截图"""
        if not HAS_PLAYWRIGHT: return None, None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(user_agent=self.user_agent, viewport={'width': 1280, 'height': 800})
                page = await context.new_page()
                await page.goto(url, wait_until='networkidle', timeout=30000)
                content = await page.content()
                screenshot_bytes = await page.screenshot(type='jpeg', quality=80)
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                await browser.close()
                return content, screenshot_base64
        except Exception as e:
            logger.error(f"[LinkReader] 截图失败: {e}")
            return None, None

    async def _fetch_url_content(self, url: str):
        """网页抓取入口"""
        if self._is_music_site(url):
            return await self._handle_music_smart_search(url), None
        
        domain = urlparse(url).netloc
        social_platforms = ["xiaohongshu.com", "zhihu.com", "weibo.com", "bilibili.com", "douyin.com", "lofter.com"]
        if any(sp in domain for sp in social_platforms) and HAS_PLAYWRIGHT:
            html, screenshot = await self._get_screenshot_and_content(url)
            if html:
                soup = BeautifulSoup(html, 'lxml')
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']): tag.decompose()
                
                # 特定平台内容提取优化
                if "xiaohongshu.com" in url:
                    content_div = soup.find(class_=re.compile(r'desc|note-content|text'))
                    content = content_div.get_text(separator='\n', strip=True) if content_div else soup.get_text(separator='\n', strip=True)
                else:
                    content = soup.get_text(separator='\n', strip=True)
                
                return self._clean_text(content), screenshot

        # 常规网页抓取
        headers = self._get_headers(domain)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10, ssl=False) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    for tag in soup(['script', 'style', 'nav', 'footer', 'header']): tag.decompose()
                    return self._clean_text(soup.get_text(separator='\n', strip=True)), None
        except Exception as e:
            return f"网页解析出错: {str(e)}", None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """拦截请求并注入 Prompt"""
        if not self.enable_plugin: return
        urls = self.url_pattern.findall(event.message_str)
        if not urls: return
        
        target_url = urls[0]
        content, screenshot_base64 = await self._fetch_url_content(target_url)

        if content:
            # 修复 input_text 属性不存在的问题，使用 req.prompt
            req.prompt += self.prompt_template.format(content=content)
            if screenshot_base64:
                req.prompt += f"\n(附带页面截图参考)\n图片：data:image/jpeg;base64,{screenshot_base64}"
            logger.info(f"[LinkReader] 内容已成功注入 Prompt")

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        """调试指令"""
        if not url: return
        yield event.plain_result(f"正在进行深度清洗解析: {url}...")
        content, screenshot = await self._fetch_url_content(url)
        msg = f"【清洗后的正文】:\n{content}"
        if screenshot: msg += "\n\n(截图获取成功)"
        yield event.plain_result(msg)

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        """状态检查指令"""
        status_msg = ["【Link Reader 插件状态】"]
        status_msg.append(f"插件启用: {'✅' if self.enable_plugin else '❌'}")
        status_msg.append(f"歌词搜索: ✅ 定向 geciyi.com (查看歌词选项)")
        status_msg.append(f"截图支持: {'✅ 已就绪' if HAS_PLAYWRIGHT else '❌ 未安装'}")
        status_msg.append(f"最大长度: {self.max_length}")
        yield event.plain_result("\n".join(status_msg))
