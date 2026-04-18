import logging
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain

logger = logging.getLogger("astrbot")
logger.info(">>> [MultimodalPDF] v2.0.2 探测模式加载启动！")

@register("astrbot_plugin_multimodal_pdf_router", "Agent", "API 对齐探测版", "2.0.2")
class MultimodalPDFRouterPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        logger.info(">>> [MultimodalPDF] 插件类实例化成功！")

    @filter.on_decorating_result()
    async def decor_handler(self, event: AstrMessageEvent):
        logger.info(">>> [MultimodalPDF] 成功拦截到装饰回调！")
        # 暂时不做 PDF，先确认能拦到
        res = event.get_result()
        if res:
            res.chain.insert(0, Plain(text="[PDF测试拦截生效] "))
