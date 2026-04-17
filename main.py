import logging
import asyncio
import aiohttp
import os
import time
import json
import re
from concurrent.futures import ThreadPoolExecutor
from playwright.async_api import async_playwright
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image, Reply, File
from astrbot.api import AstrBotConfig

logger = logging.getLogger("astrbot")

@register("astrbot_plugin_multimodal_pdf_router", "Anti-Gravity Agent", "基于‘视觉中转’链路的深度解析插件", "1.8.4")
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
        # 基础 URL 已拆分为 OCR（视觉） 和 文本（语言） 两个独立配置
        ocr_base_url = self.config.get("ocr_api_url", "https://api.deepseek.com/v1")
        text_base_url = self.config.get("text_api_url", "https://api.deepseek.com/v1")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        
        if not api_key:
            yield event.plain_result("⚠️ 请先在插件配置页面填写您的 LLM API Key！")
            return

        # 1. 提取消息内容
        question_texts = []
        image_urls = []
        segments = getattr(event.message_obj, "message", []) or getattr(event.message_obj, "components", [])
            
        quoted_texts = []
        # 输出组件数量，简化调试信息，避免在 f-string 中使用复杂的列表推导导致语法错误
        logger.info(f"[诊断_段落解析] 发现 {len(segments)} 个组件。")
        for comp in segments:
            if isinstance(comp, Plain):
                question_texts.append(comp.text)
            elif isinstance(comp, Image):
                img_url = comp.url or comp.file
                if img_url:
                    if os.path.isabs(img_url) and not img_url.startswith("file://"):
                        img_url = f"file://{img_url}"
                    image_urls.append(img_url)
            elif isinstance(comp, Reply):
                try:
                    # 增强Reply组件属性检测 - 尝试多种可能的消息ID属性
                    target_msg_id = None
                    possible_id_attrs = ['start_id', 'id', 'message_id', 'msg_id', 'reply_id', 'target_id']
                    
                    logger.info(f"[Reply调试] Reply组件所有属性: {comp.__dict__}")
                    
                    for attr in possible_id_attrs:
                        if hasattr(comp, attr):
                            attr_value = getattr(comp, attr)
                            if attr_value:
                                target_msg_id = attr_value
                                logger.info(f"[Reply调试] 找到消息ID属性 {attr}: {target_msg_id}")
                                break
                    
                    if not target_msg_id:
                        logger.warning(f"[Reply调试] 无法从Reply组件获取消息ID，跳过处理")
                        continue
                        
                    # 使用异步超时机制获取平台适配器，防止阻塞
                    platform_name = event.get_platform_name()
                    if not platform_name:
                        logger.warning(f"[Reply调试] 无法获取平台名称，跳过适配器获取")
                        continue
                    try:
                        # 在可能阻塞的同步调用上使用线程池执行，并设置超时
                        loop = asyncio.get_event_loop()
                        with ThreadPoolExecutor() as executor:
                            adapter = await asyncio.wait_for(
                                loop.run_in_executor(executor, self.context.get_platform_inst, platform_name),
                                timeout=5.0
                            )
                        # 若直接使用平台名称未能获取到适配器，尝试常见别名作为备选
                        if not adapter:
                            fallback_names = ["qq", "qq_official", "aiocqhttp"]
                            for fn in fallback_names:
                                if fn == platform_name:
                                    continue
                                try:
                                    adapter = await asyncio.wait_for(
                                        loop.run_in_executor(executor, self.context.get_platform_inst, fn),
                                        timeout=2.0
                                    )
                                    if adapter:
                                        logger.info(f"[Reply调试] 使用平台别名 '{fn}' 成功获取适配器")
                                        break
                                except Exception:
                                    # 忽略单个别名的异常，继续尝试其他别名
                                    continue
                    except asyncio.TimeoutError:
                        logger.warning(f"[Reply调试] 获取平台适配器超时 (platform={platform_name})")
                        continue
                    except Exception as e:
                        logger.error(f"[Reply调试] 获取平台适配器异常: {e}")
                        continue
                    if not adapter:
                        logger.warning(f"[Reply调试] 获取平台适配器返回 None (platform={platform_name})")
                        continue
                    
                    # 尝试多种API方法获取消息
                    msg_data = None
                    api_methods = [
                        ("get_msg", {"message_id": target_msg_id}),
                        ("get_group_msg_history", {"group_id": getattr(event, "group_id", None), "message_seq": target_msg_id}),
                        ("get_forward_msg", {"id": target_msg_id})
                    ]
                    
                    for api_name, params in api_methods:
                        try:
                            if api_name == "get_group_msg_history" and not params.get("group_id"):
                                continue
                            logger.info(f"[Reply调试] 尝试API: {api_name}, 参数: {params}")
                            msg_data = await adapter.call_api(api_name, **params)
                            if msg_data:
                                logger.info(f"[Reply调试] {api_name} 成功返回数据")
                                break
                        except Exception as api_e:
                            logger.warning(f"[Reply调试] {api_name} 调用失败: {api_e}")
                            continue
                    
                    if not msg_data:
                        logger.warning(f"[Reply调试] 所有API方法都失败，无法获取引用消息")
                        continue
                        
                    logger.info(f"[Reply调试] 最终获取的消息数据: {json.dumps(msg_data, ensure_ascii=False, default=str)[:800]}")
                    
                    # 解析消息内容 - 支持多种数据结构
                    actual_msg = None
                    if isinstance(msg_data, dict):
                        # 尝试多种可能的消息字段路径
                        msg_paths = [
                            ["message"],
                            ["data", "message"], 
                            ["data", "messages"],
                            ["messages"],
                            ["content"]
                        ]
                        
                        for path in msg_paths:
                            temp_data = msg_data
                            try:
                                for key in path:
                                    temp_data = temp_data.get(key)
                                    if temp_data is None:
                                        break
                                if temp_data is not None:
                                    actual_msg = temp_data
                                    logger.info(f"[Reply调试] 在路径 {' -> '.join(path)} 找到消息内容")
                                    break
                            except (AttributeError, TypeError):
                                continue
                    
                    if actual_msg is None:
                        logger.warning(f"[Reply调试] 无法从返回数据中提取消息内容")
                        continue
                    
                    # 处理结构化消息格式
                    if isinstance(actual_msg, list):
                        logger.info(f"[Reply调试] 处理结构化消息，共 {len(actual_msg)} 个段落")
                        for i, segment in enumerate(actual_msg):
                            if not isinstance(segment, dict): 
                                logger.warning(f"[Reply调试] 段落 {i} 不是字典格式: {type(segment)}")
                                continue
                            seg_type = segment.get("type")
                            seg_data = segment.get("data", {})
                            logger.info(f"[Reply调试] 段落 {i}: type={seg_type}, data={seg_data}")
                            
                            if seg_type == "text":
                                txt = seg_data.get("text", "")
                                if txt: 
                                    quoted_texts.append(txt)
                                    logger.info(f"[Reply调试] 提取文本: {txt[:100]}...")
                            elif seg_type == "image":
                                img_url = seg_data.get("url") or seg_data.get("file") or seg_data.get("path")
                                if img_url: 
                                    if os.path.isabs(img_url) and not img_url.startswith("file://"):
                                        img_url = f"file://{img_url}"
                                    image_urls.append(img_url)
                                    logger.info(f"[Reply调试] 提取图片: {img_url}")
                    
                    # 处理CQ码字符串格式
                    elif isinstance(actual_msg, str):
                        logger.info(f"[Reply调试] 处理CQ码字符串: {actual_msg[:200]}...")
                        
                        # 提取图片
                        cq_images = re.findall(r'\[CQ:image,([^\]]+)\]', actual_msg)
                        for params_str in cq_images:
                            try:
                                params = dict(p.split('=', 1) for p in params_str.split(',') if '=' in p)
                                img_url = params.get("url") or params.get("file") or params.get("path")
                                if img_url:
                                    if os.path.isabs(img_url) and not img_url.startswith("file://"):
                                        img_url = f"file://{img_url}"
                                    image_urls.append(img_url)
                                    logger.info(f"[Reply调试] 从CQ码提取图片: {img_url}")
                            except Exception as cq_e:
                                logger.warning(f"[Reply调试] CQ码解析失败: {cq_e}")
                        
                        # 提取纯文本
                        pure_text = re.sub(r'\[CQ:[^\]]+\]', '', actual_msg).strip()
                        if pure_text:
                            quoted_texts.append(pure_text)
                            logger.info(f"[Reply调试] 提取纯文本: {pure_text[:100]}...")
                    
                    else:
                        logger.warning(f"[Reply调试] 未知的消息格式: {type(actual_msg)}")
                        
                except Exception as e:
                    logger.error(f"[Reply调试] 提取 Reply 内容报错: {e}", exc_info=True)

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
            vision_payload = {
                "model": vision_model,
                "messages": [{"role": "user", "content": [{"type": "text", "text": vision_prompt}, *[{"type": "image_url", "image_url": {"url": url}} for url in image_urls]]}]
            }

            # 视觉 OCR 调用带重试的实现
            for attempt in range(max_retries + 1):
                try:
                    if attempt == 0:
                        yield event.plain_result(f"🔍 正在通过 {vision_model} 像素级提取细节...")
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            f"{ocr_base_url.rstrip('/')}/chat/completions",
                            json=vision_payload,
                            headers=headers,
                            timeout=90,
                        ) as resp:
                            if resp.status == 200:
                                v_data = await resp.json()
                                image_description = v_data['choices'][0]['message']['content']
                                logger.info(f"[视觉中转] OCR 识别成功，字数: {len(image_description)}")
                                break
                            elif resp.status == 429:
                                await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"视觉异常: {e}")
                    if attempt == max_retries:
                        yield event.plain_result(
                            f"⚠️ 图片识别失败（已重试 {max_retries+1} 次）：{e}\n将尝试仅基于您的文字描述进行回答。"
                        )
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
        
        combined_user_input = ""
        if quoted_texts:
            quoted_text_str = " ".join(quoted_texts).strip()
            if quoted_text_str:
                combined_user_input += f"【被引用的历史上下文】:\n{quoted_text_str}\n\n"
        combined_user_input += f"【用户的当前指令】: {question}\n【图片像素级识别记录】: {image_description}"
        text_payload = {"model": text_model, "messages": [{"role": "system", "content": final_system_prompt}, {"role": "user", "content": combined_user_input}], "response_format": {"type": "json_object"}}

        ans_json = {}
        for attempt in range(max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{text_base_url.rstrip('/')}/chat/completions",
                        json=text_payload,
                        headers=headers,
                        timeout=120,
                    ) as resp:
                        if resp.status == 200:
                            res_data = await resp.json()
                            ans_str = res_data['choices'][0]['message']['content']
                            json_match = re.search(r'\{.*\}', ans_str, re.DOTALL)
                            if json_match:
                                ans_json = json.loads(json_match.group())
                                break
                        elif resp.status == 429:
                            await asyncio.sleep(3)
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
