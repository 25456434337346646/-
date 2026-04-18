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
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image, Reply, File, Node
from astrbot.api import AstrBotConfig

logger = logging.getLogger("astrbot")

@register("astrbot_plugin_multimodal_pdf_router", "Anti-Gravity Agent", "基于‘视觉中转’链路的深度解析插件", "1.9.3")
class MultimodalPDFRouterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        import sys
        if sys.platform == "darwin":
            self.data_dir = os.path.join(os.path.expanduser("~"), "Library", "Containers", "com.tencent.qq", "Data", "tmp", "astrbot_pdf_reports")
        else:
            self.data_dir = "/AstrBot/data/pdf_reports"
        os.makedirs(self.data_dir, exist_ok=True)

    async def _render_pdf(self, html_body: str, model_name: str) -> str:
        """核心渲染引擎"""
        mathjax_config = """<script>window.MathJax={tex:{inlineMath:[['$','$'],['\\(','\\)']]},startup:{pageReady:()=>MathJax.startup.defaultPageReady().then(()=>window.MATHJAX_DONE=true)}};</script><script src="https://npm.elemecdn.com/mathjax@3.2.2/es5/tex-mml-chtml.js"></script>"""
        html_content = f"<!DOCTYPE html><html><head><meta charset='UTF-8'>{mathjax_script}<style>body{{font-family:serif;padding:30px;line-height:1.6;}} .header{{text-align:center;border-bottom:2px solid #333;margin-bottom:20px;}} .content{{font-size:14pt;word-wrap:break-word;}}</style></head><body><div class='header'><h1>{model_name} 知识简报</h1><p>{time.strftime('%Y-%m-%d %H:%M:%S')}</p></div><div class='content'>{html_body}</div></body></html>"
        
        tmp_path = os.path.join(self.data_dir, f"kb_{int(time.time())}.pdf")
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(html_content, wait_until="networkidle")
            try: await page.wait_for_function("window.MATHJAX_DONE === true", timeout=15000)
            except: pass
            await page.pdf(path=tmp_path, format="A4")
            await browser.close()
        return os.path.abspath(tmp_path)

    @filter.on_decor_message()
    async def decor_knowledge_result(self, event: AstrMessageEvent):
        """全局结果拦截：不仅拦截 Plain，更要拦截 Node 组件"""
        result = event.get_result()
        if not result or not result.chain: return

        all_text = ""
        is_kb = False
        kb_keywords = ["相关度:", "【知识", "来源:", "参考资料", "知识库", "Knowledge"]

        def extract_content(components):
            nonlocal all_text, is_kb
            for comp in components:
                if isinstance(comp, Plain):
                    txt = comp.text
                    all_text += txt + "\n"
                    if any(kw in txt for kw in kb_keywords): is_kb = True
                elif isinstance(comp, Node):
                    if hasattr(comp, 'content') and comp.content:
                        extract_content(comp.content)
                    elif hasattr(comp, 'message') and comp.message:
                        extract_content(comp.message)

        extract_content(result.chain)

        if is_kb and len(all_text) > 30:
            logger.info(f"[PDF拦截器] 监听到知识库 Node 内容，启动 PDF 转换...")
            try:
                pdf_path = await self._render_pdf(all_text.replace("\n", "<br>"), "AstrBot 知识大脑")
                result.chain = [
                    Plain(text="📄 检测到知识库结构化回复，已自动为您重制为 PDF 报告：\n"),
                    File(name="Knowledge_Report.pdf", url=f"file://{pdf_path}")
                ]
            except Exception as e:
                logger.error(f"[PDF拦截器] 转换失败: {e}")

    @filter.command("ai", alias={"ask", "解答", "解析"})
    async def handle_multimodal_query(self, event: AstrMessageEvent):
        yield event.plain_result(f"🚀 v1.9.3 已同步。")
