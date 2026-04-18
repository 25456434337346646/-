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

@register("astrbot_plugin_multimodal_pdf_router", "Anti-Gravity Agent", "基于‘视觉中转’链路的深度解析插件", "1.9.8")
class MultimodalPDFRouterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 动态环境感知
        import sys
        if sys.platform == "darwin":
            self.data_dir = os.path.join(
                os.path.expanduser("~"), "Library", "Containers",
                "com.tencent.qq", "Data", "tmp", "astrbot_pdf_reports"
            )
        else:
            self.data_dir = "/AstrBot/data/pdf_reports"
            
        os.makedirs(self.data_dir, exist_ok=True)

    async def _render_pdf(self, html_body: str, model_name: str) -> str:
        """核心渲染引擎：将 HTML 转换为 PDF"""
        mathjax_config = """<script>
window.MathJax = {
  tex: { inlineMath: [['$','$'], ['\\\\(','\\\\)']], displayMath: [['$$','$$'], ['\\\\[','\\\\]']] },
  startup: { pageReady: () => MathJax.startup.defaultPageReady().then(() => { window.MATHJAX_DONE = true; }) }
};
</script>"""
        mathjax_script = f"{mathjax_config}<script id=\"MathJax-script\" src=\"https://npm.elemecdn.com/mathjax@3.2.2/es5/tex-mml-chtml.js\"></script>"
        html_content = f"<!DOCTYPE html><html><head><meta charset='UTF-8'>{mathjax_script}<style>body{{font-family:'Times New Roman',serif;padding:40px;line-height:1.6;color:#333;}} .header{{text-align:center;border-bottom:2px solid #333;margin-bottom:20px;padding-bottom:10px;}} .content{{font-size:14pt;margin-top:20px;word-wrap:break-word;}} h1{{color:#2c3e50;}} pre{{background:#f4f4f4;padding:10px;border-radius:5px;white-space:pre-wrap;}}</style></head><body><div class='header'><h1>{model_name} 分析报告</h1><p>生成日期: {time.strftime('%Y-%m-%d %H:%M:%S')}</p></div><div class='content'>{html_body}</div></body></html>"
        
        tmp_pdf_path = os.path.join(self.data_dir, f"report_{int(time.time())}.pdf")
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(html_content, wait_until="networkidle")
            try: await page.wait_for_function("window.MATHJAX_DONE === true", timeout=20000)
            except: pass
            await asyncio.sleep(0.5)
            await page.pdf(path=tmp_pdf_path, format="A4")
            await browser.close()
        return os.path.abspath(tmp_pdf_path)

    @filter.on_decor_message()
    async def decor_knowledge_result(self, event: AstrMessageEvent):
        """全局结果拦截：使用 v1.9.8 升级版地毯式提取引擎"""
        result = event.get_result()
        if not result or not result.chain: return

        # 终极提取器
        def robust_extract(obj) -> str:
            if isinstance(obj, str): return obj + " "
            if isinstance(obj, (int, float, bool)): return str(obj) + " "
            ext = ""
            if isinstance(obj, list):
                for item in obj: ext += robust_extract(item)
            elif isinstance(obj, dict):
                for k, v in obj.items(): ext += robust_extract(v)
            elif hasattr(obj, '__dict__'):
                ext += robust_extract(obj.__dict__)
            return ext

        all_text = robust_extract(result.chain)
        
        # 指标识别
        is_kb = False
        kb_keywords = ["相关度:", "【知识", "来源:", "参考资料", "知识库", "Knowledge"]
        academic_indicators = ["\\", "$", "{", "}", "[", "]", "分解", "多项式", "特征值"]
        
        if any(kw in all_text for kw in kb_keywords): is_kb = True
        elif len(all_text) > 150 and any(indi in all_text for indi in academic_indicators): is_kb = True

        if is_kb and len(all_text) > 30:
            logger.info(f"[PDF拦截器] v1.9.8 成功捕获目标内容 (长度:{len(all_text)})，开始渲染附件...")
            try:
                formatted_body = all_text.replace("\n", "<br>")
                formatted_body = re.sub(r'```(.*?)```', r'<pre>\1</pre>', formatted_body, flags=re.DOTALL)
                pdf_path = await self._render_pdf(formatted_body, "AstrBot 学术大脑")
                result.chain = [
                    Plain(text="📄 学术分析简报已通过 v1.9.8 引擎自动整理：\n"),
                    File(name="Analysis_Report.pdf", url=f"file://{pdf_path}")
                ]
            except Exception as e:
                logger.error(f"[PDF拦截器] 转换失败: {e}")

    @filter.command("ai", alias={"ask", "解答", "解析"})
    async def handle_multimodal_query(self, event: AstrMessageEvent):
        """处理 /ai 命令逻辑 (修复版)"""
        text_api_key = self.config.get("text_api_key", ""); ocr_api_key = self.config.get("ocr_api_key", "")
        text_base_url = self.config.get("text_api_url", "https://api.deepseek.com/v1")
        ocr_base_url = self.config.get("ocr_api_url", "https://api.deepseek.com/v1")
        
        if not text_api_key or not ocr_api_key:
            yield event.plain_result("⚠️ 配置缺失！"); return

        question_texts = []; image_urls = []; pdf_urls = []
        segments = getattr(event.message_obj, "message", []) or getattr(event.message_obj, "components", [])
        for comp in segments:
            if isinstance(comp, Plain): question_texts.append(comp.text)
            elif isinstance(comp, Image): image_urls.append(comp.url or comp.file)
            elif isinstance(comp, File) and (comp.url or comp.file).lower().endswith(".pdf"):
                pdf_urls.append((comp.url or comp.file).replace("file://", ""))
        
        question = " ".join(question_texts).replace("/ai", "").replace("/ask", "").replace("/解答", "").replace("/解析", "").strip()
        
        kb_context = ""
        try:
            if question:
                retrieved = await self.context.kb_manager.retrieve(query=question)
                kb_context = self.context.kb_manager._format_context(retrieved)
        except: pass

        prompt_path = os.path.join(os.path.dirname(__file__), "system_prompt.txt")
        sys_prompt = open(prompt_path, "r", encoding="utf-8").read() if os.path.exists(prompt_path) else "你是一个学术助教。严格输出 JSON。"
        
        # 发送处理中的提示（这会被全局拦截器捕捉到并处理最终结果）
        yield event.plain_result(f"🚀 v1.9.8 正在通过视觉与知识库为您深度分析中...")
