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

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "深度修复小红书清洗逻辑，支持末尾锚点切片与正文定向提取。", "1.8.1")
class LinkReaderPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        self.general_config = self.config.get("general_config", {})
        self.enable_plugin = self.general_config.get("enable_plugin", True)
        self.max_length = self.general_config.get("max_content_length", 2000)
        self.timeout = self.general_config.get("request_timeout", 15)
        self.user_agent = self.general_config.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        self.prompt_template = self.general_config.get("prompt_template", "\n【以下是链接的具体内容，请参考该内容进行回答】：\n{content}\n")

        self.platform_cookies = self.config.get("platform_cookies", {})
        self.url_pattern = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[/\w\.-]*\??[\w=&%\.-]*')

    def _get_headers(self, domain: str = "") -> dict:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
        }
        cookie_key = next((k for k in ["xiaohongshu", "zhihu", "weibo", "bilibili", "douyin", "lofter"] if k in domain), None)
        if cookie_key and self.platform_cookies.get(cookie_key):
            headers["Cookie"] = self.platform_cookies[cookie_key]
        return headers

    def _is_music_site(self, url: str) -> bool:
        return any(domain in url for domain in ["music.163.com", "163cn.tv", "163.fm", "y.music.163.com"])

    def _filter_lyrics(self, lyrics: str) -> str:
        if not lyrics: return ""
        lyrics = lyrics.replace('\\n', '\n').replace('\\r', '')
        lines = [l.strip() for l in lyrics.split('\n') if l.strip()]
        filtered = []
        for line in lines:
            line = re.sub(r'\[\d+:\d+\.\d+\]', '', line).strip()
            if not line or (line.startswith('[') and line.endswith(']')): continue
            if ((':' in line or '：' in line) and len(line) < 35) or ' - ' in line:
                if not any(kw in line for kw in ["歌词", "Lyric", "LRC"]): continue
            filtered.append(line)
        return '\n'.join(filtered)

    def _clean_text(self, text: str) -> str:
        """强化版清洗逻辑：增加小红书特有噪音词"""
        lines = text.split('\n')
        # 增加顽固噪音黑名单
        blacklist = [
            "沪ICP备", "公网安备", "经营许可证", "版权所有", "©", "Copyright", "加载中",
            "医疗器械", "药品信息", "网信算备", "上海市互联网举报中心", "违法不良信息",
            "个性化推荐算法", "行吟信息科技", "商户合作", "自营经营者", "地址：", "电话："
        ]
        cleaned_lines = []
        for line in lines:
            line = line.strip()
            if not line or len(line) < 2 or any(kw in line for kw in blacklist):
                continue
            cleaned_lines.append(line)
        
        result = '\n'.join(cleaned_lines)
        return result[:self.max_length] + "..." if len(result) > self.max_length else result

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
                return "未找到直接歌词。"
        except: return "音乐解析失败。"

    async def _get_screenshot_and_content(self, url: str):
        if not HAS_PLAYWRIGHT: return None, None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                # 针对小红书，模拟 iPhone 以获得更纯净的移动端布局
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
                    viewport={'width': 390, 'height': 844}
                )
                page = await context.new_page()
                await page.goto(url, wait_until='networkidle', timeout=30000)
                await asyncio.sleep(3) # 增加等待时间，确保异步内容加载完毕
                content = await page.content()
                screenshot_bytes = await page.screenshot(type='jpeg', quality=85)
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                await browser.close()
                return content, screenshot_base64
        except Exception as e:
            logger.error(f"[LinkReader] 截图失败: {e}")
            return None, None

    async def _fetch_url_content(self, url: str):
        if self._is_music_site(url):
            return await self._handle_music_direct_api(url), None
        
        domain = urlparse(url).netloc
        is_xhs = any(sp in domain for sp in ["xiaohongshu.com", "xhslink.com"])
        
        if (is_xhs or any(sp in domain for sp in ["zhihu.com", "weibo.com", "bilibili.com"])) and HAS_PLAYWRIGHT:
            html, screenshot = await self._get_screenshot_and_content(url)
            if html:
                soup = BeautifulSoup(html, 'lxml')
                # 移除绝对无用的标签
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']):
                    tag.decompose()
                
                final_text = ""
                # --- 小红书定向内容提取 ---
                if is_xhs:
                    # 尝试定位正文类名
                    content_node = soup.find(class_=re.compile(r'note-content|desc|note-text'))
                    if content_node:
                        final_text = content_node.get_text(separator='\n', strip=True)
                        logger.info("[LinkReader] 小红书正文 DOM 定向提取成功")
                
                # 如果定向提取没拿到，回退到全局提取并切片
                if not final_text:
                    raw_text = soup.get_text(separator='\n', strip=True)
                    marker = "电话：9501-3888"
                    # 关键修改：使用 rfind 寻找最后一次出现的标记，切掉前面的所有噪音
                    last_index = raw_text.rfind(marker)
                    if last_index != -1:
                        final_text = raw_text[last_index + len(marker):].strip()
                        logger.info("[LinkReader] 小红书末尾锚点切片完成")
                    else:
                        final_text = raw_text

                return self._clean_text(final_text), screenshot

        # 常规网页处理
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self._get_headers(domain), timeout=10) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    for tag in soup(['script', 'style']): tag.decompose()
                    return self._clean_text(soup.get_text(separator='\n', strip=True)), None
        except: return "网页解析失败", None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enable_plugin: return
        urls = self.url_pattern.findall(event.message_str)
        if not urls: return
        content, screenshot_base64 = await self._fetch_url_content(urls[0])
        if content:
            req.prompt += self.prompt_template.format(content=content)
            if screenshot_base64:
                req.prompt += f"\n(附带页面截图)\n图片：data:image/jpeg;base64,{screenshot_base64}"

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        if not url: return
        yield event.plain_result(f"🔍 深度解析中(v1.8.1): {url}...")
        content, screenshot_base64 = await self._fetch_url_content(url)
        yield event.plain_result(f"【清洗后的正文】:\n{content}")
        if screenshot_base64:
            from astrbot.api.message_components import Image
            yield event.chain().append(Image.from_base64(screenshot_base64)).text("\n📸 以上为捕获的小红书页面截图").build()

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        msg = [
            "【Link Reader 1.8.1 状态】",
            "网易云解析: ✅",
            "小红书解析: ✅ (末尾锚点+正文定向提取)",
            f"截图引擎 (Playwright): {'✅ 已加载' if HAS_PLAYWRIGHT else '❌ 未安装'}"
        ]
        yield event.plain_result("\n".join(msg))
