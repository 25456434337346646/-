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

@register("astrbot_plugin_multimodal_pdf_router", "Anti-Gravity Agent", "支持多模态分流与友好报错的 PDF 生成插件", "1.5.1")
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
                    target_msg_id = comp.id
                    msg_data = await event.adapter.call_api("get_msg", message_id=target_msg_id)
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

        # 2. 内置大脑 Prompt：判断意图并生成内容
        # 目标：让 LLM 返回 JSON 格式，包含 mode ('chat' 或 'pdf') 和对应内容
        system_prompt = (
            "你是一个智能助手。你需要分析用户的输入（可能包含图片描述）并决定回应模式。\n"
            "1. 如果用户只是进行普通聊天、问好、或者是简单问答，请使用 'chat' 模式。\n"
            "2. 如果用户要求进行复杂的学术推导、长篇总结、数学解题且需要精美排版，请使用 'pdf' 模式。\n"
            "你的输出必须是一个合法的 JSON 字符串，格式如下：\n"
            "{\"mode\": \"chat\", \"chat_messages\": [\"回复内容1\", \"回复内容2\"]}\n"
            "或\n"
            "{\"mode\": \"pdf\", \"pdf_content\": \"HTML格式的精美报告内容\"}\n"
            "请直接返回 JSON，不要包含任何 Markdown 代码块包裹。"
        )

        user_content = [{"type": "text", "text": question}]
        for url in image_urls:
            user_content.append({"type": "image_url", "image_url": {"url": url}})

        # 选定模型：如果有图，必须使用主人填写的视觉模型
        target_model = self.config.get("llm_model", "deepseek-chat")
        if image_urls:
            target_model = self.config.get("llm_vision_model", "qwen-vl-max")
            logger.info(f"[分流引擎] 检测到图片，已自动切换至视觉模型: {target_model}")

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "response_format": {"type": "json_object"}
        }

        # 3. 发起同步请求 (内置大脑)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers) as response:
                    # 优先处理 429 频率限制错误
                    if response.status == 429:
                        yield event.plain_result("⚠️ 请求过于频繁！您的大模型 API 提供商限制了目前的访问速度。如果您使用的是免费 Key，请稍微等几分钟再试，或更换更高等级的 Key。")
                        return

                    if response.status != 200:
                        err_text = await response.text()
                        yield event.plain_result(f"❌ LLM 请求失败 ({response.status}): {err_text}")
                        return
                    
                    res_data = await response.json()
                    ans_str = res_data['choices'][0]['message']['content']
                    ans_json = json.loads(ans_str)
        except Exception as e:
            yield event.plain_result(f"🤯 思考过程发生异常: {e}")
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
