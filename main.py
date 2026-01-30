import re
import asyncio
import traceback
import base64
import json
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, quote, parse_qs

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

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "终极修复小红书导航栏清洗，支持系统依赖缺失预警与深度正文提取。", "1.8.2")
class LinkReaderPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.general_config = self.config.get("general_config", {})
        self.enable_plugin = self.general_config.get("enable_plugin", True)
        self.max_length = self.general_config.get("max_content_length", 2000)
        self.timeout = self.general_config.get("request_timeout", 15)
        self.user_agent = self.general_config.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        self.prompt_template = self.general_config.get("prompt_template", "\n【链接正文如下】：\n{content}\n")

    def _is_music_site(self, url: str) -> bool:
        return any(domain in url for domain in ["music.163.com", "163cn.tv", "163.fm", "y.music.163.com"])

    def _filter_lyrics(self, lyrics: str) -> str:
        if not lyrics: return ""
        lines = [l.strip() for l in lyrics.replace('\\n', '\n').split('\n') if l.strip()]
        filtered = []
        for line in lines:
            line = re.sub(r'\[\d+:\d+\.\d+\]', '', line).strip()
            if not line or (line.startswith('[') and line.endswith(']')): continue
            filtered.append(line)
        return '\n'.join(filtered)

    def _clean_text(self, text: str) -> str:
        """深度清洗：增加对小红书导航栏的暴力过滤"""
        # 移除这些特定的导航和冗余词汇
        blacklist = [
            "创作中心", "业务合作", "发现", "发布", "通知", "登录", "注册",
            "营业执照", "医疗器械", "网上有害信息", "违法不良信息", "加载中",
            "沪ICP备", "公网安备", "版权所有", "©", "Copyright", "地址：", "电话：", "更多", "关注"
        ]
        
        lines = text.split('\n')
        cleaned_lines = []
        for line in lines:
            line = line.strip()
            if not line or len(line) < 1 or any(kw == line for kw in blacklist):
                continue
            # 过滤包含备案号的行
            if re.search(r'备字\[\d+\]|网信算备|资格证书', line):
                continue
            cleaned_lines.append(line)
        
        result = '\n'.join(cleaned_lines)
        return result[:self.max_length]

    async def _handle_music_direct_api(self, url: str) -> str:
        try:
            async with aiohttp.ClientSession() as session:
                final_url = url
                if "163" in url:
                    async with session.head(url, allow_redirects=True, timeout=8) as resp:
                        final_url = str(resp.url)
                id_match = re.search(r'id=(\d+)', final_url) or re.search(r'song/(\d+)', final_url)
                if id_match:
                    api_url = f"https://music.163.com/api/song/lyric?id={id_match.group(1)}&lv=-1&tv=-1"
                    async with session.get(api_url, headers={"Referer": "https://music.163.com/", "User-Agent": self.user_agent}) as resp:
                        data = json.loads(await resp.text())
                        lrc = data.get("lrc", {}).get("lyric", "")
                        if lrc: return f"【网易云解析】\n\n{self._filter_lyrics(lrc)}"
                return "未找到网易云直连歌词。"
        except: return "音乐解析失败。"

    async def _get_screenshot_and_content(self, url: str):
        if not HAS_PLAYWRIGHT: return None, None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
                    viewport={'width': 390, 'height': 844}
                )
                page = await context.new_page()
                await page.goto(url, wait_until='networkidle', timeout=30000)
                await asyncio.sleep(4) # 延长等待确保 JS 渲染完毕
                content = await page.content()
                screenshot_bytes = await page.screenshot(type='jpeg', quality=85)
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                await browser.close()
                return content, screenshot_base64
        except Exception as e:
            logger.error(f"[LinkReader] 截图失败 (请检查系统依赖): {e}")
            return None, None

    async def _fetch_url_content(self, url: str):
        if self._is_music_site(url):
            return await self._handle_music_direct_api(url), None
        
        domain = urlparse(url).netloc
        is_xhs = any(sp in domain for sp in ["xiaohongshu.com", "xhslink.com"])
        
        if (is_xhs or "zhihu.com" in domain or "weibo.com" in domain) and HAS_PLAYWRIGHT:
            html, screenshot = await self._get_screenshot_and_content(url)
            if html:
                soup = BeautifulSoup(html, 'lxml')
                # 暴力清理小红书导航
                for nav in soup.select('nav, footer, .header, .footer, .sidebar'): nav.decompose()
                
                final_text = ""
                if is_xhs:
                    # 1. 尝试直接抓取正文 div
                    main_content = soup.find(class_=re.compile(r'note-content|desc|note-text'))
                    if main_content:
                        # 抓取博主名 + 正文
                        author = soup.find(class_=re.compile(r'author|user-name|nickname'))
                        author_text = f"博主：{author.get_text(strip=True)}\n" if author else ""
                        final_text = author_text + main_content.get_text(separator='\n', strip=True)
                
                if not final_text:
                    final_text = soup.get_text(separator='\n', strip=True)
                
                return self._clean_text(final_text), screenshot

        # 常规网页
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    return self._clean_text(soup.get_text(separator='\n', strip=True)), None
        except: return "解析失败", None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enable_plugin: return
        urls = self.url_pattern.findall(event.message_str)
        if not urls: return
        content, screenshot_base64 = await self._fetch_url_content(urls[0])
        if content:
            req.prompt += self.prompt_template.format(content=content)
            if screenshot_base64:
                req.prompt += f"\n(图片内容已通过视觉组件捕获)\n图片：data:image/jpeg;base64,{screenshot_base64}"

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        if not url: return
        yield event.plain_result(f"🔍 深度解析 v1.8.2: {url}")
        content, screenshot_base64 = await self._fetch_url_content(url)
        
        if screenshot_base64:
            from astrbot.api.message_components import Image
            yield event.chain().append(Image.from_base64(screenshot_base64)).text(f"\n【清洗后的正文】:\n{content}").build()
        else:
            yield event.plain_result(f"⚠️ 截图失败(请安装系统依赖)\n【清洗后的正文】:\n{content}")

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        msg = [
            "【Link Reader 1.8.2 状态报告】",
            "网易云: ✅",
            "小红书: ✅ (正文 DOM 定向提取)",
            f"截图支持: {'✅ 正常' if HAS_PLAYWRIGHT else '❌ 未就绪'}",
            "提示: 若截图失败请运行 playwright install-deps"
        ]
        yield event.plain_result("\n".join(msg))
