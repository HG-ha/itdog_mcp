import asyncio
import sys

# Set event loop policy for Windows at the very top
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import gc
import re
import time
from typing import Dict, Any, Optional, Union
from playwright.async_api import async_playwright, Page
import logging
import uuid
from bs4 import BeautifulSoup


# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("itdog_mcp")

DEFAULT_CONTEXT_OPTIONS = {
    "accept_downloads": False,
    "java_script_enabled": True,
    "bypass_csp": True,  # 绕过内容安全策略以确保截图功能正常
    "permissions": [],  # 清空所有权限
    "proxy": None,  # 禁用代理
    "extra_http_headers": { # 默认的额外HTTP头
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8", # 修改为中文优先
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document"
    }
}



HTTP_URL_PATTERN = re.compile(r'^https?://', re.IGNORECASE)
DOMAIN_PATTERN = re.compile(r'^([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:/.*)?$')
IPV4_PATTERN = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
# 修复IPv6正则：支持压缩格式 (::)
IPV6_PATTERN = re.compile(r'^\[?[0-9a-fA-F:]+::?[0-9a-fA-F:]*\]?$', re.IGNORECASE)
# 修复IPv6端口正则
IPV6_PORT_PATTERN = re.compile(r'^\[([0-9a-fA-F:]+)\]:(\d+)$', re.IGNORECASE)
IPV4_PORT_PATTERN = re.compile(r'^(\d{1,3}\.){3}\d{1,3}:\d+$')
DOMAIN_PORT_PATTERN = re.compile(r'^([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}:\d+$')
PORT_CHECK_PATTERN = re.compile(r'.+:\d+$')  # 端口检查专用

def validate_url(url, speedtype):
    """验证URL格式并根据speedtype进行类型检查"""
    # 基本格式检查
    is_http = bool(HTTP_URL_PATTERN.match(url))
    is_domain = bool(DOMAIN_PATTERN.match(url))
    is_ipv4 = bool(IPV4_PATTERN.match(url))
    
    # 修复：先检查IPv6带端口格式（因为可能包含方括号）
    is_ipv6_port = bool(IPV6_PORT_PATTERN.match(url))
    # 修复：IPv6检查排除带端口情况
    is_ipv6 = bool(IPV6_PATTERN.match(url)) and not is_ipv6_port
    
    is_ipv4_port = bool(IPV4_PORT_PATTERN.match(url))
    is_domain_port = bool(DOMAIN_PORT_PATTERN.match(url))
    
    # 类型特定检查
    if speedtype in ("ipv4ping", "ipv6ping"):
        if not is_http and not is_ipv6 and PORT_CHECK_PATTERN.match(url):
            return {"code": 400, "msg": "ping类型不支持端口号", "data": None}
    
    elif speedtype in ("ipv4tcping", "ipv6tcping"):
        if not (is_ipv4_port or is_domain_port or is_ipv6_port):
            return {"code": 400, "msg": "tcping类型必须为IP:端口、域名:端口或[IPv6]:端口", "data": None}
        if is_ipv6_port:
            ipv6_addr = IPV6_PORT_PATTERN.match(url).group(1)
            # 使用修复后的IPv6正则验证
            if not IPV6_PATTERN.match(ipv6_addr):
                return {"code": 400, "msg": "无效的IPv6地址", "data": None}
    
    # 修复1：通用格式验证增加带端口格式
    valid_formats = (
        is_http, 
        is_domain, 
        is_ipv4, 
        is_ipv6,
        is_ipv4_port,
        is_domain_port,
        is_ipv6_port
    )
    if not any(valid_formats):
        return {"code": 400, "msg": "无效的URL、域名或IP格式", "data": None}
    
    # 修复2：优化IP类型验证逻辑
    if speedtype.startswith("ipv4"):
        # 允许带端口格式
        if not (is_http or is_domain) and not (is_ipv4 or is_ipv4_port):
            return {"code": 400, "msg": "无效的IPv4地址", "data": None}
    
    if speedtype.startswith("ipv6"):
        # 允许带端口格式
        if not (is_http or is_domain) and not (is_ipv6 or is_ipv6_port):
            return {"code": 400, "msg": "无效的IPv6地址", "data": None}
    
    return None

class AsyncITDog:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._pages = {}  # 存储创建的页面
        self._last_cleanup = time.time()
        self._initialized = False
        self.ymloging = False
        
        self.device_presets = {
            "pc": {
                "width": 1920,
                "height": 1080,
                "device_scale_factor": 1,
                "is_mobile": False,
                "has_touch": False,
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36"
            },
            "phone": {
                "width": 390,
                "height": 844, # iPhone 12/13 Pro viewport
                "device_scale_factor": 3, # iPhone 12/13 Pro dsf
                "is_mobile": True,
                "has_touch": True,
                "viewport": {"width": 390, "height": 844},
                "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
            },
            "tablet": {
                "width": 1024,
                "height": 1366, # iPad Pro 12.9" portrait
                "device_scale_factor": 2,
                "is_mobile": True, # Playwright considers tablets mobile for emulation purposes
                "has_touch": True,
                "viewport": {"width": 1024, "height": 1366},
                "user_agent": "Mozilla/5.0 (iPad; CPU OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
            }
        }
        
    async def initialize(self):
        """初始化Playwright和浏览器"""
        if self._initialized:
            return
            
        try:
            self._playwright = await async_playwright().start()
            # 增加更多安全相关的浏览器参数
            browser_args = [
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "--no-sandbox",
                "--no-zygote",
                "--disable-notifications",
                "--disable-popup-blocking",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-translate",
                "--hide-scrollbars",
                "--metrics-recording-only",
                "--mute-audio",
                "--no-first-run",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-blink-features=AutomationControlled" # Added for anti-detection
            ]
            
            self._browser = await self._playwright.chromium.launch(
                headless=True,  # 默认为无头模式
                args=browser_args
            )
            self._initialized = True
            logger.info("Playwright 和浏览器已初始化")
        except Exception as e:
            logger.error(f"初始化浏览器时出错: {e}")
            raise
    
    async def close(self):
        """关闭浏览器和Playwright"""
        try:
            if self._browser:
                # 首先关闭所有页面
                for page_id in list(self._pages.keys()):
                    await self._close_page(page_id)
                    
                await self._browser.close()
                self._browser = None
            
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
                
            self._initialized = False
            logger.info("浏览器和Playwright已关闭")
        except Exception as e:
            logger.error(f"关闭浏览器时出错: {e}")
            
    async def schedule_cleanup(self):
        """定期清理资源"""
        while True:
            await asyncio.sleep(300)  # 5分钟检查一次
            try:
                await self._cleanup_resources()
            except Exception as e:
                logger.error(f"资源清理时出错: {e}")
    
    async def _cleanup_resources(self):
        """清理未使用的资源"""
        current_time = time.time()
        if current_time - self._last_cleanup < 300:  # 至少间隔5分钟
            return
            
        logger.info("开始清理浏览器资源...")
        
        # 关闭闲置的页面
        for page_id in list(self._pages.keys()):
            page_info = self._pages[page_id]
            if current_time - page_info["last_used"] > 300:  # 5分钟未使用
                await self._close_page(page_id)
        
        # 手动触发垃圾回收
        gc.collect()
        
        self._last_cleanup = current_time
    
    def _construct_selector(self, eletype: str, elevalue: str, elename: str = "") -> str:
        """
        根据元素类型、名称和值构建选择器
        
        参数:
            eletype: 元素类型 (id, class, name, xpath, css, tag, data, attr, text, canvas, iframe)
            elevalue: 元素值
            elename: 元素名称 (对于data、attr类型使用)
        
        返回:
            构建的选择器字符串
        """
        eletype = eletype.lower().strip()
        
        # 处理空值情况
        if not elevalue:
            logger.warning(f"构建选择器时元素值为空: 类型={eletype}, 名称={elename}")
            return ""
        
        try:
            if eletype == "id":
                return f"#{elevalue}"
            elif eletype == "class":
                return f".{elevalue}"
            elif eletype == "name":
                return f"[name='{elevalue}']"
            elif eletype == "xpath":
                return elevalue  # xpath直接返回值
            elif eletype == "css":
                return elevalue  # css选择器直接返回值
            elif eletype == "tag":
                return elevalue  # 标签名直接返回
            elif eletype == "data":
                attr_name = elename or "data-id"  # 默认为data-id
                return f"[{attr_name}='{elevalue}']"
            elif eletype == "attr":
                if not elename:
                    logger.warning("使用attr类型选择器时未提供属性名")
                    return ""
                return f"[{elename}='{elevalue}']"
            elif eletype == "text":
                # 使用XPath定位包含特定文本的元素
                return f"//*[contains(text(), '{elevalue}')]"
            elif eletype == "canvas":
                # 处理canvas元素
                if elevalue.lower() == "first":
                    return "canvas"  # 页面中第一个canvas
                elif elevalue.isdigit():
                    # 第n个canvas
                    index = int(elevalue) - 1
                    return f"canvas:nth-of-type({index + 1})"
                else:
                    # 按ID或选择器查找
                    if elevalue.startswith("#") or elevalue.startswith("."):
                        return elevalue
                    return f"canvas#{elevalue}"
            elif eletype == "iframe":
                # 处理iframe元素
                if elevalue.lower() == "first":
                    return "iframe"  # 页面中第一个iframe
                elif elevalue.isdigit():
                    # 第n个iframe
                    index = int(elevalue) - 1
                    return f"iframe:nth-of-type({index + 1})"
                else:
                    # 按ID或选择器查找
                    if elevalue.startswith("#") or elevalue.startswith("."):
                        return elevalue
                    return f"iframe#{elevalue}"
            else:
                logger.warning(f"不支持的元素类型: {eletype}")
                return ""
        except Exception as e:
            logger.error(f"构建选择器时发生错误: {e}")
            return ""

            
    async def _create_page(self, device_config=None, custom_js: Optional[str] = None):
        if not self._browser:
            logger.error("浏览器未初始化。请先调用 initialize()。")
            raise RuntimeError("浏览器未初始化")

        context_options = DEFAULT_CONTEXT_OPTIONS.copy()
        if device_config:
            context_options.update({
                "user_agent": device_config.get("user_agent"),
                "viewport": device_config.get("viewport"),
                "device_scale_factor": device_config.get("device_scale_factor"),
                "is_mobile": device_config.get("is_mobile"),
                "has_touch": device_config.get("has_touch"),
            })
        
        # 为每个页面创建独立的浏览器上下文
        browser_context = await self._browser.new_context(**context_options)
        page = await browser_context.new_page()
        page_id = str(uuid.uuid4())
        self._pages[page_id] = {"page": page, "context": browser_context, "created_at": time.time()}

        try:
            # 注入通用的反检测脚本
            await page.add_init_script("""
                // --- WebDriver Flag ---
                try {
                    if (navigator.webdriver || Navigator.prototype.hasOwnProperty('webdriver')) {
                        delete Navigator.prototype.webdriver; // Try to delete it from prototype
                    }
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => false,
                        configurable: true
                    });
                } catch (e) {
                    console.warn('Failed to spoof navigator.webdriver: ' + e.toString());
                }

                // --- Spoof Navigator Properties ---
                try {
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['zh-CN', 'zh', 'en-US', 'en'], // Default Accept-Language is zh-CN,zh;q=0.9,en;q=0.8
                        configurable: true
                    });
                } catch (e) {
                    console.warn('Failed to spoof navigator.languages: ' + e.toString());
                }

                try {
                    Object.defineProperty(navigator, 'platform', {
                        get: () => 'Win32', // Common platform, consider making dynamic based on UA
                        configurable: true
                    });
                } catch (e) {
                    console.warn('Failed to spoof navigator.platform: ' + e.toString());
                }

                // --- Plugins and MimeTypes ---
                try {
                    const MOCK_PLUGIN_ARRAY = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', mimeTypes: [] },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', mimeTypes: [] },
                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '', mimeTypes: [] }
                    ];

                    const MOCK_MIME_TYPE_ARRAY = [
                        { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: MOCK_PLUGIN_ARRAY[0] },
                        { type: 'application/pdf', suffixes: 'pdf', description: '', enabledPlugin: MOCK_PLUGIN_ARRAY[1] },
                        { type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable', enabledPlugin: MOCK_PLUGIN_ARRAY[2] },
                        { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable', enabledPlugin: MOCK_PLUGIN_ARRAY[2] }
                    ];


                    MOCK_PLUGIN_ARRAY[0].mimeTypes.push(MOCK_MIME_TYPE_ARRAY[0]);
                    MOCK_PLUGIN_ARRAY[1].mimeTypes.push(MOCK_MIME_TYPE_ARRAY[1]);
                    MOCK_PLUGIN_ARRAY[2].mimeTypes.push(MOCK_MIME_TYPE_ARRAY[2], MOCK_MIME_TYPE_ARRAY[3]);

                    MOCK_PLUGIN_ARRAY.forEach(p => { Object.freeze(p.mimeTypes); Object.freeze(p); });
                    Object.freeze(MOCK_MIME_TYPE_ARRAY);

                    Object.defineProperty(navigator, 'plugins', { get: () => MOCK_PLUGIN_ARRAY, configurable: true });
                    Object.defineProperty(navigator, 'mimeTypes', { get: () => MOCK_MIME_TYPE_ARRAY, configurable: true });
                } catch (e) {
                    console.warn('Failed to spoof plugins/mimeTypes: ' + e.toString());
                }

                // --- Spoof WebGL ---
                try {
                    const getParameterOld = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(parameter) {
                        // UNMASKED_VENDOR_WEBGL (0x9245)
                        if (parameter === 37445) return 'Intel Open Source Technology Center';
                        // UNMASKED_RENDERER_WEBGL (0x9246)
                        if (parameter === 37446) return 'Mesa DRI Intel(R) Ivybridge Mobile ';
                        // VENDOR (0x1F00)
                        if (parameter === 7936) return 'Intel Open Source Technology Center';
                        // RENDERER (0x1F01)
                        if (parameter === 7937) return 'Mesa DRI Intel(R) Ivybridge Mobile ';
                        
                        if (getParameterOld.apply) {
                            return getParameterOld.apply(this, arguments);
                        }
                        return null; // Fallback
                    };
                    // Hide the override from toString
                    WebGLRenderingContext.prototype.getParameter.toString = getParameterOld.toString.bind(getParameterOld);
                } catch (e) {
                    console.warn('Failed to spoof WebGL: ' + e.toString());
                }

                // --- Permissions API ---
                try {
                    const originalPermissionsQuery = navigator.permissions.query;
                    navigator.permissions.query = (parameters) => {
                        try {
                            if (parameters.name === 'notifications') {
                                return Promise.resolve({ state: Notification.permission || 'default' });
                            }
                            if (originalPermissionsQuery.call) {
                                return originalPermissionsQuery.call(navigator.permissions, parameters);
                            }
                            return Promise.reject(new Error('Original permissions.query not callable.'));
                        } catch (e) {
                            console.warn('navigator.permissions.query inner failed: ' + e.toString());
                            return Promise.reject(e);
                        }
                    };
                    navigator.permissions.query.toString = originalPermissionsQuery.toString.bind(originalPermissionsQuery);
                } catch (e) {
                    console.warn('Failed to spoof navigator.permissions.query: ' + e.toString());
                }

                // --- Notification Permission ---
                try {
                    if (typeof Notification !== 'undefined' && Notification.permission) {
                        Object.defineProperty(Notification, 'permission', {
                            get: () => 'default', // 'denied' in headless often, 'default' is more neutral
                            configurable: true
                        });
                    }
                } catch (e) {
                    console.warn('Failed to spoof Notification.permission: ' + e.toString());
                }

                // --- Other Navigator Properties ---
                try {
                    if (navigator.deviceMemory === undefined || navigator.deviceMemory === 0) {
                        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
                    }
                    if (navigator.hardwareConcurrency === undefined || navigator.hardwareConcurrency === 0) {
                        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 4, configurable: true });
                    }
                } catch (e) {
                    console.warn('Failed to spoof deviceMemory/hardwareConcurrency: ' + e.toString());
                }

                // --- User's existing event listeners and overrides (integrated) ---
                window.addEventListener('beforeunload', (event) => {
                    event.preventDefault();
                    event.returnValue = "Navigation blocked";
                });
                document.addEventListener('contextmenu', event => event.preventDefault());
                document.addEventListener('selectstart', event => event.preventDefault());
                window.open = function() { return null; };

            """)

            # 注意：custom_js 现在不在这里执行，而是在页面加载完成后执行
            
            return page_id, page
        except Exception as e:
            logger.error(f"创建页面 {page_id} 时出错: {e}")
            # 如果出错，确保关闭已创建的页面和上下文
            if page_id in self._pages:
                await self._close_page_internal(page_id) # 使用内部关闭方法避免锁问题
            raise

    async def _get_page(self, page_id):
        """获取现有页面"""
        page_info = self._pages.get(page_id)
        if page_info:
            # 更新最后使用时间
            page_info["last_used"] = time.time()
            return page_info["page"]
        return None
    
    async def _close_page(self, page_id):
        """关闭页面及其上下文"""
        page_info = self._pages.pop(page_id, None) # 使用pop确保移除
        if page_info:
            try:
                await page_info["page"].close()
                if page_info.get("context"): # 关闭与页面关联的上下文
                    await page_info["context"].close()
                logger.info(f"页面 {page_id} 及其上下文已关闭")
            except Exception as e:
                logger.error(f"关闭页面 {page_id} 或其上下文时出错: {e}")
            # finally: # pop已经移除了元素
            #     pass 
            
    async def _navigate_to_url(self, page: Page, url: str, wait_until: str = "domcontentloaded", max_retries: int = 3, wait_for_resources: bool = False) -> bool:
        """
        导航到URL，包含重试机制和改进的安全措施
        
        参数:
            page: Playwright页面对象
            url: 要导航到的URL
            wait_until: 导航完成判断标准，可以是 'domcontentloaded', 'load', 'networkidle'
            max_retries: 最大重试次数
            wait_for_resources: 是否等待页面所有资源加载完成
        """
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "http://" + url
        for attempt in range(max_retries):
            try:                # 设置请求拦截（允许所有常见资源类型）
                await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "stylesheet", "image", "font", "xhr", "fetch", "media", "texttrack", "eventsource", "manifest", "other"] else route.abort())
                
                # 导航到页面，根据wait_for_resources参数决定等待策略
                actual_wait_until = "networkidle" if wait_for_resources else wait_until
                response = await page.goto(
                    url, 
                    wait_until=actual_wait_until, 
                    timeout=90000
                )
                
                if not response:
                    logger.warning(f"导航到 {url} 没有响应对象 (尝试 {attempt+1}/{max_retries})")
                    if attempt == max_retries - 1:
                        return False
                    continue
                
                # 检查响应状态
                if response.ok:
                    # 等待页面稳定
                    await asyncio.sleep(1)
                    
                    # 注入额外的安全措施
                    await page.evaluate("""

                        // 禁用alert等对话框
                        window.alert = window.confirm = window.prompt = function() {};
                        
                        // 禁用一些可能导致问题的API
                        window.print = function() {};
                        window.find = function() {};
                        window.requestFileSystem = function() {};
                    """)
                    
                    return True
                else:
                    logger.warning(f"导航到 {url} 响应状态: {response.status} (尝试 {attempt+1}/{max_retries})")
                
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"导航到 {url} 时出错: {e}")
                if attempt == max_retries - 1:
                    return False
                
        return False
        
    def _get_device_config(self, device, width="", height=""):
        """获取设备配置"""
        # 首先获取基础配置
        if device in self.device_presets:
            config = self.device_presets[device].copy()
        else:
            # 如果设备类型不存在，使用PC配置作为基础
            config = self.device_presets["pc"].copy()
        
        # 应用自定义宽高（如果提供）
        try:
            if width and height:
                width_int = int(width)
                height_int = int(height)
                if width_int > 0 and height_int > 0:
                    config.update({
                        "width": width_int,
                        "height": height_int,
                        "viewport": {
                            "width": width_int,
                            "height": height_int
                        },
                        "device_scale_factor": 1  # 自定义尺寸时使用1:1的缩放比
                    })
        except (ValueError, TypeError):
            pass
            
        return config

    async def _screenshot_canvas(self, page: Page, selector: str) -> bytes:
        """
        截取canvas元素的内容
        
        参数:
            page: Playwright页面对象
            selector: 指向canvas元素的选择器
            
        返回:
            canvas元素的截图数据(bytes)或None
        """
        try:
            # 等待并获取canvas元素
            canvas = await page.wait_for_selector(selector, state="visible", timeout=30000)
            
            if not canvas:
                logger.error(f"未找到canvas元素: {selector}")
                return None
                
            # 先确保元素在视口中
            await canvas.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)  # 滚动后短暂等待
            
            # 尝试使用JavaScript获取canvas的内容为base64编码的数据URL
            data_url = await page.evaluate("""(selector) => {
                const canvas = document.querySelector(selector);
                if (!canvas || !(canvas instanceof HTMLCanvasElement)) {
                    return null;
                }
                // 尝试获取canvas内容为PNG格式的dataURL
                try {
                    return canvas.toDataURL('image/png');
                } catch (e) {
                    // 如果canvas是跨域的，toDataURL可能会失败
                    console.error('无法获取canvas内容:', e);
                    return null;
                }
            }""", selector)
            
            if data_url:
                # 从data URL提取base64编码的图像数据
                # 格式为: "data:image/png;base64,..."
                base64_data = data_url.split(',')[1]
                import base64
                image_bytes = base64.b64decode(base64_data)
                return image_bytes
            else:
                # 如果无法通过JavaScript获取，退回到对元素的常规截图
                logger.warning(f"无法通过JavaScript获取canvas内容，使用元素截图代替: {selector}")
                return await canvas.screenshot(type="png")
                
        except Exception as e:
            logger.error(f"截取canvas内容时出错: {str(e)}")
            return None
            
    async def normalize_traceroute_keys(self,rows,contype):
        key_map = {
                "traceroute": {
                    "跳数": "hop",
                    "IP": "ip",
                    "PTR": "ptr",
                    "地理位置 /仅供参考": "loc",
                    "AS": "as",
                    "丢包率": "loss",
                    "发包": "pkt",
                    "最新(ms)": "last",
                    "最快(ms)": "best",
                    "最慢(ms)": "worst",
                    "平均(ms)": "avg",
                    "最 快(ms)": "best",
                },
                "speedtest": {
                    "区域/运营商": "region",
                    "区域": "region",
                    "最快": "fast",
                    "最慢": "slow",
                    "平均": "avg",
                    "检测点": "point",
                    "响应IP": "rip",
                    "IP位置": "iploc",
                    "状态": "status",
                    "总耗时": "duration",
                    "解析": "analysis",
                    "连接": "conn",
                    "下载": "down",
                    "重定向": "redir",
                    "Head": "head",
                    "响应IP:端口": "rip_port",
                    "响应时间": "response_time",
                    "丢包": "loss",
                    "发包": "pkt",
                },
                
            }
        normalized = []
        for row in rows:
            new_row = {}
            for k, v in row.items():
                nk = key_map[contype].get(k.strip(), k.strip())
                new_row[nk] = v
            normalized.append(new_row)
        return normalized
    
    # 从element中查找表格并解析为json
    async def _find_table_in_element(self, element,speedtype):
        html = await element.inner_html()
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")

        if speedtype == "ipv4web" or speedtype == "ipv6web":
            # 获取表头
            headers = ["operator","point"]
            tables_th = table.find("thead").find_all("th")
            for th in tables_th:
                _th = th.get_text(strip=True)
                headers.append(_th)

            # 获取数据行
            rows = []
            tables_tr = table.find("tbody").find_all("tr")
            for tr in tables_tr:
                row = {}
                tr_class = tr.attrs.get("class")
                if tr_class and 'node_tr' in tr_class:
                    tds = tr.find_all("td")
                    for i, td in enumerate(tds):
                        _td = td.get_text(strip=True)
                        _td_class = td.attrs.get("class")
                        if _td == "查看":
                            # row[headers[-1]] = 'HEad'
                            pass
                        elif tds.index(td) == 0:
                            op,po = td.get_text(strip=False).split()
                            row[headers[i]] = op.strip()
                            row[headers[i + 1]] = po.strip()
                            pass
                        # 处理响应IP
                        elif _td_class and "real_ip" in _td_class:
                            # 获取第一个div的值
                            row[headers[i + 2]] = td.get_text(strip=False).split()[0]
                        else:
                            row[headers[i + 2]] = _td
                    rows.append(row)
                if tr_class and 'head_info' in tr_class:
                    rows[len(rows) - 1].update({"Head": "\n".join(tr.find_all("td")[0].stripped_strings)})
                    continue
            
            return await self.normalize_traceroute_keys(rows, 'speedtest')
        
        elif speedtype == "ipv4traceroute" or speedtype == "ipv6traceroute":
            headers = []
            tables_th = table.find("thead").find_all("th")
            for th in tables_th:
                _th = th.get_text(strip=True)
                headers.append(_th)
            rows = []
            tables_tr = table.find("tbody").find_all("tr", class_="ttl_tr")
            for tr in tables_tr:
                row = {}
                tds = tr.find_all("td")
                for i, td in enumerate(tds):
                    row[headers[i]] = td.get_text(strip=True)
                rows.append(row)
            return await self.normalize_traceroute_keys(rows, "traceroute")
        
        elif speedtype == "overview":
            headers = []
            tables_th = table.find("thead").find_all("th")
            for th in tables_th:
                _th = th.get_text(strip=True)
                headers.append(_th)
            rows = []
            tables_tr = table.find("tbody").find_all("tr")
            for tr in tables_tr:
                row = {}
                tds = tr.find_all("td")
                for i, td in enumerate(tds):
                    row[headers[i]] = td.get_text(strip=True)
                rows.append(row)
            return await self.normalize_traceroute_keys(rows, "speedtest")
        
        else:
            headers = ["operator","point"]
            tables_th = table.find("thead").find_all("th")
            for th in tables_th:
                _th = th.get_text(strip=True)
                headers.append(_th)
            rows = []
            tables_tr = table.find("tbody").find_all("tr")
            for tr in tables_tr:
                row = {}
                tds = tr.find_all("td")
                for i, td in enumerate(tds):
                    _td = td.get_text(strip=True)
                    _td_class = td.attrs.get("class")
                    if tds.index(td) == 0:
                        op,po = td.get_text(strip=False).split()
                        row[headers[i]] = op.strip()
                        row[headers[i + 1]] = po.strip()
                    elif _td_class and "real_ip" in _td_class:
                        row[headers[i + 2]] = td.get_text(strip=False).split()[0]
                    else:
                        row[headers[i + 2]] = td.get_text(strip=True)
                rows.append(row)
            return await self.normalize_traceroute_keys(rows, "speedtest")

    
    # 从element中提取域名解析数据到json
    async def _find_dns_in_element(self,element):
        html = await element.inner_html()
        soup = BeautifulSoup(html, "html.parser")
        result = []
        for li in soup.select("ul.ip_list li"):
            ip = li.find("span", class_="ml-3")
            percent = li.find("span", class_="text-primary")
            if ip and percent:
                result.append({"ip": ip.text.strip(), "percent": percent.text.strip()})
        return result

    # itdog网站测速
    async def itdog_speedtest(self, url: str, speedtype: Union[str] = "ipv4web", dns: str = "", node: str = "") -> Dict[str, Any]:
        """
        使用itdog网站测速
        
        参数:
            url: 要测速的网站
            speedtype: 测速类型，支持 "ipv4ping", "ipv4tcping", "ipv4web", "ipv4traceroute", "ipv6ping", "ipv6tcping", "ipv6web", "ipv6traceroute"
            dns: 可选的DNS服务器
        """

        typedict = {
            'ipv4ping':{"url":"https://www.itdog.cn/ping/","js":f'document.getElementById("host").value="{url}";check_form("fast");setTimeout(function(){{}},1e4);'},
            'ipv4tcping':{"url":"https://www.itdog.cn/tcping/","js":f'document.getElementById("host").value="{url}";check_form("fast");setTimeout(function(){{}},1e4);'},
            'ipv4web':{"url":"https://www.itdog.cn/http/","js":f'document.getElementById("host").value="{url}";check_form("fast");setTimeout(function(){{}},1e4);'},
            'ipv4traceroute':{"url":"https://www.itdog.cn/traceroute/","js":f'document.getElementById("host").value="{url}";check_form("fast");setTimeout(function(){{}},1e4);'},
            'ipv6ping':{"url":"https://www.itdog.cn/ping_ipv6/","js":f'document.getElementById("host").value="{url}";check_form("fast");setTimeout(function(){{}},1e4);'},
            'ipv6tcping':{"url":"https://www.itdog.cn/tcping_ipv6/","js":f'document.getElementById("host").value="{url}";check_form("fast");setTimeout(function(){{}},1e4);'},
            'ipv6web':{"url":"https://www.itdog.cn/http_ipv6/","js":f'document.getElementById("host").value="{url}";check_form("fast");setTimeout(function(){{}},1e4);'},
            'ipv6traceroute':{"url":"https://www.itdog.cn/traceroute_ipv6/","js":f'document.getElementById("host").value="{url}";check_form("fast");setTimeout(function(){{}},1e4);'},
        }

        # 测速后移除广告的js
        ad_remove_js = '$(".gg_link").remove();document.querySelector(".page-header").style.display = "none";'

        # 如果指定了dns，则使用指定dns
        dnsjs = f"""document.querySelector('input[name="dns_server_type"][value="custom"]').click();document.getElementById('dns_server').value = '{dns}';"""

        # 判断测速类型
        if speedtype not in typedict:
            return {"code": 400, "msg": "不支持的测速类型", "data": None}
        
        if speedtype == "ipv4traceroute" or speedtype == "ipv6traceroute":
            if not node:
                return {"code": 500, "msg": "请选择测试节点", "data": None}
        
        # 校验url，只允许ipv4、ipv6、域名、http的url格式
        if not url:
            return {"code": 400, "msg": "URL不能为空", "data": None}
        validation_result = validate_url(url, speedtype)
        if validation_result:
            return validation_result
        
        # 中国地区测速结果概览, 海外地区需要执行js
        overview_xpath = '//*[@id="pills-tabContent"]'
        # 中国地区测速结果表格数据
        zh_overview_table_xpath = '//*[@id="china_region"]'
        # 海外地区测速结果表格数据
        overview_table_xpath = '//*[@id="global_region"]'
        # 海外地区测速结果切换js
        convert_js = '''document.querySelector('a.nav-link[id="pills-profile-tab"]').click();'''
        # 域名解析统计
        dns_stats_xpath = '//*[@id="screenshots"]/div/div/div/div[4]/div/div'
        # 所有检测点测试结果
        all_points_xpath = '//*[@id="return_info"]/div/div'
        # 测速进度条
        progress_bar_xpath = '//*[@id="complete_progress"]/div'
        # traceroute测试节点
        traceroute_node_xpath = '//*[@id="screenshots"]/div/div/div/div[3]/div/div/div[3]/div[1]/select'
        # traceroute节点测试结果
        traceroute_xpath = '//*[@id="tracert_result"]/div'
        
        # 开始截图
        page_id = None
        
        try:
            device_config = self._get_device_config('pc')
            
            page_id, page = await self._create_page(device_config=device_config)

            navigate_success = await self._navigate_to_url(page, typedict[speedtype]["url"], wait_until="load")
            if not navigate_success:
                raise Exception(f"导航到 {typedict[speedtype]['url']} 失败")
            
            # 开始执行测速逻辑
            # 首先选择dns
            if dns:
                await page.evaluate(dnsjs)

            # 判断是否为traceroute
            if speedtype == "ipv4traceroute" or speedtype == "ipv6traceroute":
                
                # 判断节点是否存在
                nodelist = await page.query_selector(traceroute_node_xpath)
                nodedata = await nodelist.text_content()

                if node not in nodedata:
                    return {"code": 500, "msg": "节点不存在", "data": None}

                # 选择节点
                await page.evaluate('''
                    $('.node_select').val(function() {
                    return $(this).find('option').filter(function() {
                        return $(this).text().trim() === 'optionText';
                    }).val();
                    }).trigger('change');'''.replace("optionText",node))
            
            # 在页面上执行测速js
            await page.evaluate(typedict[speedtype]["js"])
            # 等待页面加载
            await asyncio.sleep(1)
            # 等待测速结果加载，如果10秒内没有测速完成，则直接当做测速完成，如果测速进度条为100%，则认为测速完成
            start_time = time.time()
            while True:
                if time.time() - start_time > 10:
                    break
                
                progress_bar = await page.query_selector(progress_bar_xpath)
                if progress_bar:
                    progress_valuenow = await progress_bar.get_attribute("aria-valuenow")
                    progress_valuemax = await progress_bar.get_attribute("aria-valuemax")
                    # 测速完成
                    if progress_valuenow == progress_valuemax:
                        break

                await asyncio.sleep(0.1)
            
            # 执行移除广告的js
            await page.evaluate(ad_remove_js)
            await asyncio.sleep(0.5)  # 等待js执行完成

            results = {}

            if speedtype == "ipv4traceroute" or speedtype == "ipv6traceroute":
                traceroute_element = await page.query_selector(traceroute_xpath)

                if traceroute_element:
                    json_data = await self._find_table_in_element(traceroute_element, speedtype)
                    results['traceroute'] = json_data
                else:
                    return {"code": 500, "msg": "测速失败", "data": None}
            else:
                # 所有检测点测试结果
                # all_points_element = await page.query_selector(all_points_xpath)
                # if all_points_element:
                #     json_data = await self._find_table_in_element(all_points_element,speedtype)
                #     results['all_points'] = json_data

                # overview_xpath
                zh_overview_element = await page.query_selector(overview_xpath)
                if zh_overview_element:
                    zh_overview_element_data = await page.query_selector(zh_overview_table_xpath)
                    json_data = await self._find_table_in_element(zh_overview_element_data, "overview")
                    results['zh_overview'] = json_data

                # 海外地区测速结果概览
                await page.evaluate(convert_js)
                await asyncio.sleep(0.5)  # 等待切换完成

                overview_element = await page.query_selector(overview_xpath)
                if overview_element:
                    overview_element_data = await page.query_selector(overview_table_xpath)
                    json_data =  await self._find_table_in_element(overview_element_data, "overview")
                    results['overview'] = json_data

                # 域名解析统计
                dns_stats_element = await page.query_selector(dns_stats_xpath)
                if dns_stats_element:
                    json_data = await self._find_dns_in_element(dns_stats_element)
                    results['dns_stats'] = json_data

            return {"code": 200, "msg": "success", "data": results}
            

        except Exception as e:
            logger.error(e)
            return {"code": 500, "msg": "error", "data": None}
        finally:
            if page_id:
                await self._close_page(page_id)

    async def get_traceroute_nodes(self, node_type: str = "ipv4") -> Dict[str, Any]:
        """
        获取traceroute可用的测试节点列表
        
        参数:
            node_type: 节点类型，支持 "ipv4" 或 "ipv6"
        
        返回:
            包含节点信息的字典
        """
        
        # 根据类型选择URL
        if node_type == "ipv4":
            target_url = "https://www.itdog.cn/traceroute/"
        elif node_type == "ipv6":
            target_url = "https://www.itdog.cn/traceroute_ipv6/"
        else:
            return {"code": 400, "msg": "不支持的节点类型，仅支持ipv4或ipv6", "data": None}
        
        # 节点选择器xpath
        node_selector_xpath = '//*[@id="screenshots"]/div/div/div/div[3]/div/div/div[3]/div[1]/select'
        
        page_id = None
        
        try:
            device_config = self._get_device_config('pc')
            page_id, page = await self._create_page(device_config=device_config)

            # 导航到对应的traceroute页面
            navigate_success = await self._navigate_to_url(page, target_url, wait_until="load")
            if not navigate_success:
                raise Exception(f"导航到 {target_url} 失败")
            
            # 等待页面加载完成
            await asyncio.sleep(1)
            
            # 获取节点选择器元素
            node_selector = await page.wait_for_selector(node_selector_xpath, state="attached", timeout=10000)
            if not node_selector:
                return {"code": 500, "msg": "未找到节点选择器", "data": None}
            
            # 使用JavaScript提取节点信息
            nodes_data = await page.evaluate('''
                () => {
                    const selector = document.querySelector('select.node_select');
                    if (!selector) return null;
                    
                    const result = {};
                    const optgroups = selector.querySelectorAll('optgroup');
                    
                    optgroups.forEach(optgroup => {
                        const groupName = optgroup.getAttribute('label');
                        result[groupName] = [];
                        
                        const options = optgroup.querySelectorAll('option');
                        options.forEach(option => {
                            result[groupName].push(option.textContent.trim());
                        });
                    });
                    
                    return result;
                }
            ''')
            
            if not nodes_data:
                return {"code": 500, "msg": "获取节点数据失败", "data": None}
            
            # 统计节点数量
            total_nodes = 0
            for group in nodes_data.values():
                total_nodes += len(group)
            
            return {
                "code": 200, 
                "msg": "success", 
                "data": {
                    "node_type": node_type,
                    "total_nodes": total_nodes,
                    "groups": nodes_data
                }
            }
            
        except Exception as e:
            logger.error(f"获取{node_type}节点列表时出错: {e}")
            return {"code": 500, "msg": f"获取节点列表失败: {str(e)}", "data": None}
        finally:
            if page_id:
                await self._close_page(page_id)


# 如果直接运行此文件，执行测试
if __name__ == "__main__":
    pass
