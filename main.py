import logging
import asyncio
import aiohttp
import os
import time
import json
import re
from concurrent.futures import ThreadPoolExecutor
from playwright.async_api import async_playwright
from pdf2image import convert_from_path
import tempfile
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image, Reply, File, Node
from astrbot.api import AstrBotConfig

logger = logging.getLogger("astrbot")
logger.info(">>> [MultimodalPDF] 核心对齐修复版 v2.0.1 正在加载...")

@register("astrbot_plugin_multimodal_pdf_router", "Anti-Gravity Agent", "基于核心 API 对齐的 PDF 拦截插件", "2.0.1")
class MultimodalPDFRouterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 环境初始化...
        import sys
        if sys.platform == "darwin":
            self.data_dir = os.path.join(os.path.expanduser("~"), "Library", "Containers", "com.tencent.qq", "Data", "tmp", "astrbot_pdf_reports")
        else:
            self.data_dir = "/AstrBot/data/pdf_reports"
        os.makedirs(self.data_dir, exist_ok=True)

    async def _render_pdf(self, html_body: str, model_name: str) -> str:
        mathjax_script = """<script>window.MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']]},startup:{pageReady:()=>MathJax.startup.defaultPageReady().then(()=>window.MATHJAX_DONE=true)}};</script><script src="https://npm.elemecdn.com/mathjax@3.2.2/es5/tex-mml-chtml.js"></script>"""
        html_content = f"<!DOCTYPE html><html><head><meta charset='UTF-8'>{mathjax_script}<style>body{{font-family:serif;padding:30px;line-height:1.6;}} .header{{text-align:center;border-bottom:2px solid #333;margin-bottom:20px;}} .content{{font-size:14pt;word-wrap:break-word;}}</style></head><body><div class='header'><h1>{model_name} 报告</h1><p>{time.strftime('%Y-%m-%d %H:%M:%S')}</p></div><div class='content'>{html_body}</div></body></html>"
        tmp_path = os.path.join(self.data_dir, f"kb_{int(time.time())}.pdf")
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(html_content, wait_until="networkidle")
            try: await page.wait_for_function("window.MATHJAX_DONE === true", timeout=10000)
            except: pass
            await page.pdf(path=tmp_path, format="A4")
            await browser.close()
        return os.path.abspath(tmp_path)

    def _robust_extract(self, obj) -> str:
        if isinstance(obj, str): return obj + " "
        if isinstance(obj, (int, float, bool)): return str(obj) + " "
        ext = ""
        if isinstance(obj, list):
            for item in obj: ext += self._robust_extract(item)
        elif isinstance(obj, dict):
            for v in obj.values(): ext += self._robust_extract(v)
        elif hasattr(obj, 'chain'): ext += self._robust_extract(obj.chain)
        elif hasattr(obj, 'text'): ext += self._robust_extract(obj.text)
        elif hasattr(obj, '__dict__'): ext += self._robust_extract(obj.__dict__)
        return ext

    # 关键点修复：使用正确的 API 钩子名称
    @filter.on_decorating_result()
    async def decor_handler(self, event: AstrMessageEvent):
        res = event.get_result()
        if not res or not res.chain: return
        
        all_text = self._robust_extract(res.chain)
        logger.info(f"[2.0.1拦截器] 监听到回复内容，长度: {len(all_text)}")
        
        kb_keywords = ["相关度:", "【知识", "来源:", "知识库", "谱分解", "极小多项式"]
        if any(kw in all_text for kw in kb_keywords) or (len(all_text) > 150 and (any(i in all_text for i in ["$", "\\", "{", "}"]))):
            logger.info(f"[2.0.1拦截器] 命中学术意图，正在封锁消息并强制转化为 PDF...")
            try:
                pdf_path = await self._render_pdf(all_text.replace("\n", "<br>"), "AstrBot 学术引擎")
                res.chain = [
                    Plain(text="📄 深度学术/知识库回复已通过 2.0.1 核心钩子拦截，报告已生成：\n"),
                    File(name="Analysis_Report.pdf", url=f"file://{pdf_path}")
                ]
            except Exception as e:
                logger.error(f"[2.0.1拦截器] 转换失败: {e}")

    @filter.command("test2")
    async def handle_test(self, event: AstrMessageEvent):
        yield event.plain_result("✅ v2.0.1 核心 API (on_decorating_result) 已对齐。")
