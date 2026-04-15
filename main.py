import os
import uuid
import json
import asyncio
import aiohttp
import logging
from playwright.async_api import async_playwright

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image, Reply, File

logger = logging.getLogger("astrbot")

# =================================================================================
# [环境级约束与物理部署要求 (Mac mini M4)]
# 1. 渲染引擎：本项目已升级至 Playwright，不再依赖 wkhtmltopdf。
# 2. 读写权限白名单：必须将运行此脚本终端 (如 iTerm / Terminal) 或 Python 环境
#    加入“完全磁盘访问权限”(系统设置 -> 隐私与安全性)-> Full Disk Access 白名单。
# 3. 防止休眠机制：考虑到异步服务在 M4 长期运行，必须关闭系统设置中的“使硬盘进入睡眠”选项！
# =================================================================================

# 插件配置 (主人可以在此处手动修改)
LLM_API_URL = "http://127.0.0.1:8000/v1/bot/route_intent" # 后端大模型解析接口 URL
DELAY_BETWEEN_CHAT = 1.5                                    # Chat 模式下消息发送间隔（秒）

@register("astrbot_plugin_multimodal_pdf_router", "Anti-Gravity Agent", "基于意图路由与多模态能力的PDF生成双轨插件 (Playwright版)", "1.3.2")
class MultimodalPDFRouterPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = os.path.join(os.getcwd(), "data")
        os.makedirs(self.data_dir, exist_ok=True)

    # 终极修复：经嗅探，当前版本仅支持 command 监听。
    # 用户现在可以通过 /ai [问题] 或在 @机器人 后输入 /ai 来触发。
    @filter.command("ai", alias={"ask", "解答", "解析"})
    async def handle_multimodal_query(self, event: AstrMessageEvent):
        """核心交互流主函数"""
        question_texts = []
        image_urls = []

        # 1. 遍历并提炼事件组件
        # 在旧版 AstrBot v4.x 中，消息字段名为 .message 而非 .components
        segments = getattr(event.message_obj, "message", [])
        if not segments:
            segments = getattr(event.message_obj, "components", [])
            
        for comp in segments:
            if isinstance(comp, Plain):
                question_texts.append(comp.text)
            elif isinstance(comp, Image):
                if hasattr(comp, "url") and comp.url:
                    image_urls.append(comp.url)
            elif isinstance(comp, Reply):
                try:
                    target_msg_id = comp.id
                    msg_data = await event.adapter.call_api("get_msg", message_id=target_msg_id)
                    if msg_data and isinstance(msg_data, dict):
                        if "message" in msg_data:
                            for segment in msg_data["message"]:
                                if isinstance(segment, dict) and segment.get("type") == "image":
                                    img_url = segment.get("data", {}).get("url")
                                    if img_url:
                                        image_urls.append(img_url)
                except Exception as e:
                    logger.error(f"[多模态解析] 提取 Reply 组件中的引用图片出现故障: {e}")

        question = " ".join(question_texts).strip()
        
        if not question and not image_urls:
            await event.reply("未检测到有效意图，请@我时发送具体的文本或相应的图片素材。")
            return
            
        payload = {
            "question": question,
            "image_urls": image_urls
        }
        
        ans_json = {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(LLM_API_URL, json=payload, timeout=90) as response:
                    if response.status == 200:
                        ans_json = await response.json()
                    else:
                        await event.reply(f"远程系统异常，链路返回状态码: {response.status}")
                        return
        except Exception as e:
            logger.error(f"[通信模块] 向大语言模型发出调度请求失败: {e}")
            await event.reply("后端 LLM 的路由 API 访问失败，请检查网络链路与 API URL 配置。")
            return

        mode = ans_json.get("mode", "chat")
        
        if mode == "chat":
            chat_messages = ans_json.get("chat_messages", [])
            for idx, msg in enumerate(chat_messages):
                await event.reply(msg)
                if idx < len(chat_messages) - 1:
                    await asyncio.sleep(DELAY_BETWEEN_CHAT)
                    
        elif mode == "pdf":
            await event.reply("收到，该问题涉及复杂推导，正在为您生成格式化 PDF 报告...")
            
            raw_pdf_content = ans_json.get("pdf_content", "")
            html_content = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{{font-family: sans-serif; padding: 20px; line-height: 1.6;}} img {{max-width: 100%;}}</style></head><body>{raw_pdf_content}</body></html>"
            
            filename = f"out_{uuid.uuid4().hex}.pdf"
            tmp_pdf_path = os.path.join(self.data_dir, filename)
            
            try:
                # 使用 Playwright 进行异步 PDF 渲染
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    context = await browser.new_context()
                    page = await context.new_page()
                    
                    # 设置页面内容并等待加载完成
                    await page.set_content(html_content, wait_until="networkidle")
                    
                    # 生成 PDF
                    await page.pdf(path=tmp_pdf_path, format="A4", print_background=True, margin={"top": "20px", "bottom": "20px", "left": "20px", "right": "20px"})
                    
                    await browser.close()
                
                # 发送文件
                await event.reply(File(tmp_pdf_path))
                
            except Exception as e:
                logger.error(f"[渲染模块] Playwright 生成 PDF 失败：{e}")
                await event.reply("PDF 生成引擎发生异常，请联系主人检查 Playwright 环境是否正确安装。")
            finally:
                if os.path.exists(tmp_pdf_path):
                    try:
                        os.remove(tmp_pdf_path)
                    except Exception as rm_e:
                        logger.error(f"严重: PDF 缓存文件删除失败: {rm_e}")
                        
        else:
            await event.reply(f"发现异常，模型指令下发了无法支持的基础模式: {mode}。")
