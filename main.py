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

# 只要加载就打印，确认物理存活
logger.info(">>> [MultimodalPDF] 插件主程序物理加载成功！版本: v2.0.0")

@register("astrbot_plugin_multimodal_pdf_router", "Anti-Gravity Agent", "基于‘视觉中转’链路的深度解析插件", "2.0.0")
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
        mathjax_script = """<script>window.MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']]},startup:{pageReady:()=>MathJax.startup.defaultPageReady().then(()=>window.MATHJAX_DONE=true)}};</script><script src="https://npm.elemecdn.com/mathjax@3.2.2/es5/tex-mml-chtml.js"></script>"""
        html_content = f"<!DOCTYPE html><html><head><meta charset='UTF-8'>{mathjax_script}<style>body{{font-family:serif;padding:30px;line-height:1.6;}} .header{{text-align:center;border-bottom:2px solid #333;margin-bottom:20px;}} .content{{font-size:14pt;word-wrap:break-word;}}</style></head><body><div class='header'><h1>{model_name} 知识报告</h1><p>{time.strftime('%Y-%m-%d %H:%M:%S')}</p></div><div class='content'>{html_body}</div></body></html>"
        
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
        """全局地毯式提取文字"""
        if isinstance(obj, str): return obj + " "
        if isinstance(obj, (int, float, bool)): return str(obj) + " "
        ext = ""
        if isinstance(obj, list):
            for item in obj: ext += self._robust_extract(item)
        elif isinstance(obj, dict):
            for v in obj.values(): ext += self._robust_extract(v)
        elif hasattr(obj, 'chain'):
            ext += self._robust_extract(obj.chain)
        elif hasattr(obj, 'text'):
            ext += self._robust_extract(obj.text)
        elif hasattr(obj, '__dict__'):
            ext += self._robust_extract(obj.__dict__)
        return ext

    @filter.on_decor_message()
    async def decor_handler(self, event: AstrMessageEvent):
        """保留原装饰器，增加探测日志"""
        res = event.get_result()
        if not res: return
        text = self._robust_extract(res.chain)
        logger.info(f"[Decor监听到] 消息长度:{len(text)}")
        return await self._process_kb_to_pdf(event, text)

    # 增加更底层的处理器，应对流式逃逸
    async def handle_event(self, event: AstrMessageEvent):
        """覆盖基类处理器，在最底层进行监视"""
        ret = await super().handle_event(event) # 正常处理
        return ret

    async def _process_kb_to_pdf(self, event: AstrMessageEvent, all_text: str):
        kb_keywords = ["相关度:", "【知识", "来源:", "知识库"]
        academic_indicators = ["\\", "$", "{", "}", "分解", "多项式"]
        
        if any(kw in all_text for kw in kb_keywords) or (len(all_text) > 120 and any(indi in all_text for indi in academic_indicators)):
            logger.info(f"[PDF拦截器] 发现学术/知识库目标，启动转换...")
            try:
                pdf_path = await self._render_pdf(all_text.replace("\n", "<br>"), "AstrBot 视觉大脑")
                # 暴力破解：通过 event 直接修改回传链
                result = event.get_result()
                if result:
                    result.chain = [
                        Plain(text="📄 知识库深度内容解析已就绪：\n"),
                        File(name="Analysis_Report.pdf", url=f"file://{pdf_path}")
                    ]
            except Exception as e:
                logger.error(f"[PDF拦截器] 报错: {e}")

    @filter.command("ai")
    async def handle_ai(self, event: AstrMessageEvent):
        yield event.plain_result("🚀 v2.0.0 全底层监视已开启。")
