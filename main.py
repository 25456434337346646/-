import logging
import asyncio
import aiohttp
import os
import time
import json
from playwright.async_api import async_playwright
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image, Reply, File
from astrbot.api import AstrBotConfig

logger = logging.getLogger("astrbot")

@register("astrbot_plugin_multimodal_pdf_router", "Anti-Gravity Agent", "基于‘视觉中转’链路的深度解析插件", "1.6.7")
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
        
        # 0.1 分流逻辑：有图用视觉模型，没图用文本模型
        has_images = False # 待解析
        # ------------------
        
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
                    # 在 v4.23.1 的 Reply 组件中，消息 ID 可能存储在 start_id 或 id 字段中
                    target_msg_id = getattr(comp, "start_id", getattr(comp, "id", None))
                    if not target_msg_id:
                        continue
                        
                    adapter = self.context.get_platform_inst(event.get_platform_name())
                    if adapter:
                        msg_data = await adapter.call_api("get_msg", message_id=target_msg_id)
                        if msg_data and "message" in msg_data:
                            for segment in msg_data["message"]:
                                if isinstance(segment, dict) and segment.get("type") == "image":
                                    img_url = segment.get("data", {}).get("url")
                                    if img_url: image_urls.append(img_url)
                except Exception as e:
                    logger.error(f"[多模态解析] 提取图片报错: {e}")

        question = " ".join(question_texts).replace("/ai", "").replace("/ask", "").replace("/解答", "").replace("/解析", "").strip()
        
        # --- 极简版指令介绍 ---
        if not question and not image_urls or question.lower() in ["help", "帮助", "功能"]:
            help_text = (
                "可用指令: /ai, /ask, /解析, /解答\n"
                "用法示例:\n"
                "1. /ai 问答内容 (直接提问)\n"
                "2. /ai [图片] (解析当前图片)\n"
                "3. [回复某张图片] + /ai (解析历史图片)\n"
                "注意: 请确保已在管理界面配置 API Key。"
            )
            yield event.plain_result(help_text)
            return
        # --------------------

        # --- 视觉中转链路核心逻辑 ---
        image_description = ""
        if image_urls:
            vision_api_key = self.config.get("llm_api_key", "") # 默认共用 key，也可根据需求扩展
            vision_model = self.config.get("llm_vision_model", "qwen-vl-max")
            
            vision_prompt = "请精准提取图片中的所有文字信息、数学公式、逻辑关系或语法标注。只需返回识别出的原文内容，不要进行多余的分析。"
            vision_payload = {
                "model": vision_model,
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": vision_prompt},
                        *[{"type": "image_url", "image_url": {"url": url}} for url in image_urls]
                    ]}
                ]
            }
            
            try:
                yield event.plain_result(f"🔍 正在通过 {vision_model} 提取图片细节...")
                async with aiohttp.ClientSession() as session:
                    async with session.post(f"{base_url.rstrip('/')}/chat/completions", json=vision_payload, headers=headers) as resp:
                        if resp.status == 200:
                            v_data = await resp.json()
                            image_description = v_data['choices'][0]['message']['content']
                            logger.info(f"[视觉中转] OCR 识别成功，字数: {len(image_description)}")
                        else:
                            yield event.plain_result(f"❌ 视觉模型提取失败 ({resp.status})，将尝试仅基于文本回答。")
            except Exception as e:
                logger.error(f"视觉中转阶段异常: {e}")

        # 2. 调度“逻辑大脑”（文本模型）进行最终裁决
        text_model = self.config.get("llm_model", "deepseek-chat")
        
        final_system_prompt = (
            "你是一个具备顶尖逻辑分析能力的智能助手。\n"
            "你会收到用户的原始提问以及（如果有的话）从图片中提取出的 OCR 参考内容。\n"
            "你的任务是：结合这些信息，给出深度回答，并决定输出模式。\n"
            "输出必须为 JSON 格式：\n"
            "{\"mode\": \"chat\", \"chat_messages\": [\"回答摘要\", \"详细解析\"]}\n"
            "或\n"
            "{\"mode\": \"pdf\", \"pdf_content\": \"HTML格式的深度学术报告\"}"
        )
        
        combined_user_input = f"用户问题: {question}\n图片识别内容记录: {image_description}"
        
        text_payload = {
            "model": text_model,
            "messages": [
                {"role": "system", "content": final_system_prompt},
                {"role": "user", "content": combined_user_input}
            ],
            "response_format": {"type": "json_object"}
        }

        # 3. 发起最终逻辑请求
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base_url.rstrip('/')}/chat/completions", json=text_payload, headers=headers, timeout=120) as response:
                    if response.status == 429:
                        yield event.plain_result("⚠️ 逻辑模型请求频繁，请稍后再试。")
                        return
                    if response.status != 200:
                        yield event.plain_result(f"❌ 逻辑大脑响应失败 ({response.status})")
                        return
                    
                    res_data = await response.json()
                    ans_str = res_data['choices'][0]['message']['content']
                    ans_json = json.loads(ans_str)
        except Exception as e:
            yield event.plain_result(f"🤯 逻辑分析阶段异常: {e}")
            return

        # 4. 执行路由分发
        mode = ans_json.get("mode", "chat")
        
        if mode == "chat":
            msgs = ans_json.get("chat_messages", ["主人，我暂时不知道怎么回答。"])
            for idx, m in enumerate(msgs):
                yield event.plain_result(m)
                if idx < len(msgs) - 1:
                    await asyncio.sleep(self.config.get("delay_between_chat", 1.5))
        
        elif mode == "pdf":
            yield event.plain_result("🚀 发现核心意图，正在为您整理精美 PDF 报告...")
            raw_pdf_content = ans_json.get("pdf_content", "")
            html_content = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{{font-family: sans-serif; padding: 20px;}} img{{max-width:100%}}</style></head><body>{raw_pdf_content}</body></html>"
            
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
        else:
            yield event.plain_result("模型返回了未知的处理模式。")
