import logging
import asyncio
import aiohttp
import os
import time
import json
import re
from playwright.async_api import async_playwright
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image, Reply, File
from astrbot.api import AstrBotConfig

logger = logging.getLogger("astrbot")

@register("astrbot_plugin_multimodal_pdf_router", "Anti-Gravity Agent", "基于‘视觉中转’链路的深度解析插件", "1.7.5")
class MultimodalPDFRouterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = os.path.join(os.getcwd(), "data", "plugins", "multimodal_pdf_router")
        os.makedirs(self.data_dir, exist_ok=True)

    @filter.command("ai", alias={"ask", "解答", "解析"})
    async def handle_multimodal_query(self, event: AstrMessageEvent):
        """内置大脑的交互逻辑：直接调用 LLM 并根据意图路由"""
        
        # 0. 获取配置
        api_key = self.config.get("llm_api_key", "")
        base_url = self.config.get("llm_base_url", "https://api.deepseek.com/v1")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        
        if not api_key:
            yield event.plain_result("⚠️ 请先在插件配置页面填写您的 LLM API Key！")
            return

        # 1. 提取消息内容
        question_texts = []
        image_urls = []
        segments = getattr(event.message_obj, "message", []) or getattr(event.message_obj, "components", [])
            
        for comp in segments:
            if isinstance(comp, Plain):
                question_texts.append(comp.text)
            elif isinstance(comp, Image):
                image_urls.append(comp.url)
            elif isinstance(comp, Reply):
                try:
                    target_msg_id = getattr(comp, "start_id", getattr(comp, "id", None))
                    if not target_msg_id: continue
                    adapter = self.context.get_platform_inst(event.get_platform_name())
                    if adapter:
                        msg_data = await adapter.call_api("get_msg", message_id=target_msg_id)
                        if msg_data and "message" in msg_data:
                            for segment in msg_data["message"]:
                                if isinstance(segment, dict) and segment.get("type") == "image":
                                    seg_data = segment.get("data", {})
                                    img_url = seg_data.get("url") or seg_data.get("file") or seg_data.get("path")
                                    if img_url: 
                                        if os.path.isabs(img_url) and not img_url.startswith("file://"):
                                            img_url = f"file://{img_url}"
                                        image_urls.append(img_url)
                except Exception as e:
                    logger.error(f"[多模态解析] 提取图片报错: {e}")

        question = " ".join(question_texts).replace("/ai", "").replace("/ask", "").replace("/解答", "").replace("/解析", "").strip()
        
        if not question and not image_urls or question.lower() in ["help", "帮助", "功能"]:
            help_text = "可用指令: /ai, /ask, /解析, /解答\n用法示例:\n1. /ai 问答内容\n2. /ai [图片]\n3. [回复图片] + /ai"
            yield event.plain_result(help_text)
            return

        # --- 视觉提取逻辑（带重试） ---
        image_description = ""
        if image_urls:
            vision_model = self.config.get("llm_vision_model", "qwen-vl-max")
            vision_prompt = (
                "## 核心指令：像素级学术 OCR 转录\n"
                "1. **禁止概括**：原文转录图片中的所有文字，严禁忽略任何细节。\n"
                "2. **数学公式强求**：所有数学符号必须使用 LaTeX 格式完整转录。\n"
                "3. **逻辑关系**：保留题目的层级关系。\n"
                "4. **原文输出**：直接输出识别内容，不要分析。"
            )
            vision_payload = {
                "model": vision_model,
                "messages": [{"role": "user", "content": [{"type": "text", "text": vision_prompt}, *[{"type": "image_url", "image_url": {"url": url}} for url in image_urls]]}]
            }
            
            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    if attempt == 0: yield event.plain_result(f"🔍 正在通过 {vision_model} 像素级提取细节...")
                    async with aiohttp.ClientSession() as session:
                        async with session.post(f"{base_url.rstrip('/')}/chat/completions", json=vision_payload, headers=headers, timeout=90) as resp:
                            if resp.status == 200:
                                v_data = await resp.json()
                                image_description = v_data['choices'][0]['message']['content']
                                logger.info(f"[视觉中转] OCR 识别成功，字数: {len(image_description)}")
                                break
                            elif resp.status == 429: await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"视觉异常: {e}")
                    await asyncio.sleep(1)

        # --- 逻辑大脑逻辑（带重试与正则解析） ---
        text_model = self.config.get("llm_model", "deepseek-chat")
        final_system_prompt = (
            "你是一个学术级智能助教。结合 OCR 内容补全题目背景。格式要求：使用精美的 HTML/LaTeX。核心推导强制进入 pdf 模式。\n"
            "严格输出 JSON：{\"mode\": \"chat\", \"chat_messages\": [...]} 或 {\"mode\": \"pdf\", \"pdf_content\": \"HTML内容\"}"
        )
        combined_user_input = f"【用户指令】: {question}\n【图片像素级识别记录】: {image_description}"
        text_payload = {"model": text_model, "messages": [{"role": "system", "content": final_system_prompt}, {"role": "user", "content": combined_user_input}], "response_format": {"type": "json_object"}}

        ans_json = {}
        for attempt in range(max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(f"{base_url.rstrip('/')}/chat/completions", json=text_payload, headers=headers, timeout=120) as resp:
                        if resp.status == 200:
                            res_data = await resp.json()
                            ans_str = res_data['choices'][0]['message']['content']
                            json_match = re.search(r'\{.*\}', ans_str, re.DOTALL)
                            if json_match:
                                ans_json = json.loads(json_match.group())
                                break
                        elif resp.status == 429: await asyncio.sleep(3)
            except Exception as e:
                if attempt == max_retries:
                    yield event.plain_result(f"🤯 逻辑分析深度超限: {e}")
                    return
                await asyncio.sleep(2)

        # 4. 执行路由分发
        mode = ans_json.get("mode", "chat")
        if mode == "chat":
            msgs = ans_json.get("chat_messages", ["主人，识别内容不足以给出完整解答。"])
            for idx, m in enumerate(msgs):
                yield event.plain_result(m)
                if idx < len(msgs) - 1: await asyncio.sleep(1.5)
        elif mode == "pdf":
            yield event.plain_result("🚀 发现核心意图，正在为您整理精美 PDF 报告...")
            raw_pdf_content = ans_json.get("pdf_content", "")
            mathjax_script = '<script src="https://polyfill.io/v3/polyfill.min.js?features=es6"></script><script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>'
            html_content = f"<!DOCTYPE html><html><head><meta charset='UTF-8'>{mathjax_script}<style>body{{font-family: 'Times New Roman', serif; padding: 40px; line-height: 1.6; color: #333;}} .header{{text-align: center; border-bottom: 2px solid #333; margin-bottom: 20px; padding-bottom: 10px;}} .content{{font-size: 14pt; margin-top: 20px;}} h1, h2{{color: #2c3e50;}}</style></head><body><div class='header'><h1>学术深度解析报告</h1><p>生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}</p></div><div class='content'>{raw_pdf_content}</div></body></html>"
            
            tmp_pdf_path = os.path.join(self.data_dir, f"report_{int(time.time())}.pdf")
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch()
                    page = await browser.new_page()
                    await page.set_content(html_content)
                    await page.pdf(path=tmp_pdf_path, format="A4")
                    await browser.close()
                yield event.file_result(tmp_pdf_path)
            except Exception as pe:
                yield event.plain_result(f"PDF 渲染失败: {pe}")
            finally:
                if os.path.exists(tmp_pdf_path): os.remove(tmp_pdf_path)
