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
        # NapCat 运行在 macOS App Sandbox (com.tencent.qq) 中，
        # 只能访问其容器内的文件，必须将 PDF 放在容器内部
        self.data_dir = os.path.join(
            os.path.expanduser("~"), "Library", "Containers",
            "com.tencent.qq", "Data", "tmp", "astrbot_pdf_reports"
        )
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
                        if msg_data:
                            actual_msg = msg_data.get("message") or msg_data.get("data", {}).get("message")
                            if isinstance(actual_msg, list):
                                for segment in actual_msg:
                                    if isinstance(segment, dict) and segment.get("type") == "image":
                                        seg_data = segment.get("data", {})
                                        img_url = seg_data.get("url") or seg_data.get("file") or seg_data.get("path")
                                        if img_url: 
                                            if os.path.isabs(img_url) and not img_url.startswith("file://"):
                                                img_url = f"file://{img_url}"
                                            image_urls.append(img_url)
                            elif isinstance(actual_msg, str):
                                cq_images = re.findall(r'\[CQ:image,([^\]]+)\]', actual_msg)
                                for params_str in cq_images:
                                    params = dict(p.split('=', 1) for p in params_str.split(',') if '=' in p)
                                    img_url = params.get("url") or params.get("file") or params.get("path")
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

        max_retries = 2
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
                    if attempt == max_retries:
                        yield event.plain_result(f"⚠️ 图片识别失败（已重试 {max_retries+1} 次）：{e}\n将尝试仅基于您的文字描述进行回答。")
                    else:
                        await asyncio.sleep(1)

        # --- 逻辑大脑逻辑（带重试与正则解析） ---
        text_model = self.config.get("llm_model", "deepseek-chat")
        
        # 强制读取 system_prompt.txt 以确保“取消闲聊”指令生效
        prompt_path = os.path.join(os.path.dirname(__file__), "system_prompt.txt")
        if os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as f:
                final_system_prompt = f.read()
        else:
            final_system_prompt = "你是一个学术助教。严格输出 JSON：{\"mode\": \"pdf\", \"pdf_content\": \"HTML内容\"}"
        
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

        # 4. 执行路由分发 (主人：已取消闲聊模式，强制 PDF 化)
        mode = ans_json.get("mode", "pdf")
        pdf_content = ans_json.get("pdf_content", "")
        
        # 逻辑合并：即便模型返回了 chat 模式，也将其内容包装进 PDF 报告中
        if mode == "chat" or not pdf_content:
            msgs = ans_json.get("chat_messages", ["暂无详细分析内容。"])
            if not isinstance(msgs, list): msgs = [str(msgs)]
            chat_to_html = "".join([f"<p>{m}</p>" for m in msgs])
            pdf_content = f"<h2>内容交互简报</h2><div style='background:#f9f9f9;padding:15px;border-radius:8px;'>{chat_to_html}</div>"
        
        # 进入 PDF 渲染流程
        yield event.plain_result("🚀 发现核心意图，正在为您整理精美 PDF 报告...")
        raw_pdf_content = pdf_content
        mathjax_config = """<script>
window.MathJax = {
  tex: { inlineMath: [['$','$'], ['\\\\(','\\\\)']], displayMath: [['$$','$$'], ['\\\\[','\\\\]']] },
  startup: {
    pageReady: () => {
      return MathJax.startup.defaultPageReady().then(() => {
        window.MATHJAX_DONE = true;
      });
    }
  }
};
</script>"""
        mathjax_script = f"{mathjax_config}<script id=\"MathJax-script\" src=\"https://npm.elemecdn.com/mathjax@3.2.2/es5/tex-mml-chtml.js\"></script>"
        html_content = f"<!DOCTYPE html><html><head><meta charset='UTF-8'>{mathjax_script}<style>body{{font-family: 'Times New Roman', serif; padding: 40px; line-height: 1.6; color: #333;}} .header{{text-align: center; border-bottom: 2px solid #333; margin-bottom: 20px; padding-bottom: 10px;}} .content{{font-size: 14pt; margin-top: 20px;}} h1, h2{{color: #2c3e50;}}</style></head><body><div class='header'><h1>由 {text_model} 生成回答</h1><p>生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}</p></div><div class='content'>{raw_pdf_content}</div></body></html>"
        
        tmp_pdf_path = os.path.join(self.data_dir, f"report_{int(time.time())}.pdf")
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                # 设定稍微长一点的超时时间防止加载页面就直接抛出
                await page.set_content(html_content, wait_until="networkidle", timeout=60000)
                
                # 精准等待渲染完成信号
                await page.wait_for_function("window.MATHJAX_DONE === true", timeout=30000)
                await asyncio.sleep(0.5) # 额外缓冲
                await page.pdf(path=tmp_pdf_path, format="A4")
                await browser.close()
            # 使用 file:// 协议发送，NapCat 沙箱内可直接访问
            abs_pdf_path = os.path.abspath(tmp_pdf_path)
            yield event.chain_result([
                File(name=os.path.basename(tmp_pdf_path), url=f"file://{abs_pdf_path}")
            ])
        except Exception as pe:
            yield event.plain_result(f"PDF 渲染失败: {pe}")
