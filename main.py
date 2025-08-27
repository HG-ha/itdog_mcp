import asyncio
import sys
import json
from typing import Any, Dict, Optional, Union
import logging
import uuid
import time
import argparse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from mcp.server import Server
from mcp.types import (
    Tool, 
    TextContent, 
    CallToolRequest, 
    CallToolResult,
    ListToolsRequest
)

from itdog import AsyncITDog

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("itdog_mcp_server")

# 全局ITDog实例和节点列表
itdog_instance = None
ipv4_nodes = {}  # 改为字典格式，按组存储
ipv6_nodes = {}  # 改为字典格式，按组存储

async def startup_event():
    """应用启动时执行"""
    global itdog_instance, ipv4_nodes, ipv6_nodes
    
    logger.info("ITDog MCP Server 正在启动...")
    
    # 在Windows上设置事件循环策略
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    # 初始化itdog实例并获取节点列表
    try:
        logger.info("正在初始化ITDog实例...")
        itdog_instance = AsyncITDog()
        await itdog_instance.initialize()
        logger.info("ITDog 实例已初始化")
        
        # 获取IPv4节点列表
        logger.info("正在获取IPv4节点列表...")
        ipv4_result = await itdog_instance.get_traceroute_nodes(node_type="ipv4")
        if ipv4_result.get("code") == 200 and ipv4_result.get("data"):
            # 新的数据结构处理
            node_data = ipv4_result.get("data")
            if isinstance(node_data, dict) and "groups" in node_data:
                ipv4_nodes = node_data.get("groups", {})
                total_nodes = node_data.get("total_nodes", 0)
                logger.info(f"获取到 {total_nodes} 个IPv4节点，分 {len(ipv4_nodes)} 组")
            else:
                # 兼容旧格式
                ipv4_nodes = {"未分类": ipv4_result.get("data", [])}
                logger.info(f"获取到 {len(ipv4_nodes.get('未分类', []))} 个IPv4节点")
        else:
            logger.warning(f"获取IPv4节点失败: {ipv4_result.get('msg', '未知错误')}")
        
        # 获取IPv6节点列表
        logger.info("正在获取IPv6节点列表...")
        ipv6_result = await itdog_instance.get_traceroute_nodes(node_type="ipv6")
        if ipv6_result.get("code") == 200 and ipv6_result.get("data"):
            # 新的数据结构处理
            node_data = ipv6_result.get("data")
            if isinstance(node_data, dict) and "groups" in node_data:
                ipv6_nodes = node_data.get("groups", {})
                total_nodes = node_data.get("total_nodes", 0)
                logger.info(f"获取到 {total_nodes} 个IPv6节点，分 {len(ipv6_nodes)} 组")
            else:
                # 兼容旧格式
                ipv6_nodes = {"未分类": ipv6_result.get("data", [])}
                logger.info(f"获取到 {len(ipv6_nodes.get('未分类', []))} 个IPv6节点")
        else:
            logger.warning(f"获取IPv6节点失败: {ipv6_result.get('msg', '未知错误')}")
    
    except Exception as e:
        logger.error(f"初始化过程中出错: {e}")

async def cleanup():
    """清理资源"""
    global itdog_instance
    if itdog_instance:
        try:
            await itdog_instance.close()
            logger.info("ITDog 实例已关闭")
        except Exception as e:
            logger.error(f"关闭ITDog实例时出错: {e}")

async def shutdown_event():
    """应用关闭时执行"""
    logger.info("ITDog MCP Server 正在关闭...")
    await cleanup()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动
    await startup_event()
    yield
    # 关闭
    await shutdown_event()

# 创建FastAPI应用，使用新的lifespan参数
app = FastAPI(
    title="ITDog MCP Server",
    description="使用itdog网站进行网络测速测试的MCP服务器",
    version="0.1.0",
    lifespan=lifespan
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 创建MCP服务器实例
server = Server("itdog-mcp")

# Pydantic模型
class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None  # 允许字符串和整数类型
    method: str
    params: Optional[Dict[str, Any]] = None
    
    class Config:
        extra = "allow"  # 允许额外的字段

class MCPResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None  # 允许字符串和整数类型
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

# 添加测试请求模型
class ITDogTestRequest(BaseModel):
    url: str
    speedtype: str = "ipv4web"
    dns: str = ""
    node: str = ""

@server.list_tools()
async def handle_list_tools(request: ListToolsRequest) -> list[Tool]:
    """返回可用的工具列表"""
    global ipv4_nodes, ipv6_nodes
    logger.info("收到工具列表请求")
    
    # 构建节点列表字符串
    ipv4_nodes_str = "\n**IPv4可用节点:**"
    ipv6_nodes_str = "\n**IPv6可用节点:**"
    
    # 处理IPv4节点，按组显示
    if ipv4_nodes:
        for group, nodes in ipv4_nodes.items():
            if nodes:  # 确保有节点
                ipv4_nodes_str += f"\n\n{group}:\n"
                # 每组最多显示5个节点
                for i, node in enumerate(nodes[:5]):
                    ipv4_nodes_str += f"- {node}\n"
                if len(nodes) > 5:
                    ipv4_nodes_str += f"- ...等共 {len(nodes)} 个节点\n"
    else:
        ipv4_nodes_str += "\n暂无IPv4节点数据"
    
    # 处理IPv6节点，按组显示
    if ipv6_nodes:
        for group, nodes in ipv6_nodes.items():
            if nodes:  # 确保有节点
                ipv6_nodes_str += f"\n\n{group}:\n"
                # 每组最多显示5个节点
                for i, node in enumerate(nodes[:5]):
                    ipv6_nodes_str += f"- {node}\n"
                if len(nodes) > 5:
                    ipv6_nodes_str += f"- ...等共 {len(nodes)} 个节点\n"
    else:
        ipv6_nodes_str += "\n暂无IPv6节点数据"
    
    return [
        Tool(
            name="itdog_network_test",
            description=f"""
使用itdog网站进行网络测速测试，支持ping、tcping、web测试和traceroute等多种测试类型，支持IPv4和IPv6

在使用traceroute时需要指定测试节点，请选择以下节点之一：
{ipv4_nodes_str}
{ipv6_nodes_str}

测速类型说明：
- ipv4ping/ipv6ping: 使用ICMP协议测试网络连通性和延迟
- ipv4tcping/ipv6tcping: 使用TCP协议测试指定端口的连通性和延迟
- ipv4web/ipv6web: 测试网页访问速度和加载时间
- ipv4traceroute/ipv6traceroute: 追踪网络路由路径，需要指定测试节点

请不要伪造任何数据，否则可能会导致测试失败或结果不准确
""",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要测试的URL、域名或IP地址"
                    },
                    "speedtype": {
                        "type": "string",
                        "enum": [
                            "ipv4ping", "ipv4tcping", "ipv4web", "ipv4traceroute",
                            "ipv6ping", "ipv6tcping", "ipv6web", "ipv6traceroute"
                        ],
                        "default": "ipv4web",
                        "description": "测速类型"
                    },
                    "dns": {
                        "type": "string",
                        "default": "",
                        "description": "可选的DNS服务器地址"
                    },
                    "node": {
                        "type": "string", 
                        "default": "",
                        "description": "traceroute测试时需要指定的测试节点名称，如'广东广州电信'"
                    }
                },
                "required": ["url"]
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(request: CallToolRequest) -> CallToolResult:
    """处理工具调用"""
    global itdog_instance
    
    logger.info(f"收到工具调用请求: {request.name}")
    
    try:
        # 初始化itdog实例
        if itdog_instance is None:
            logger.info("正在初始化ITDog实例...")
            itdog_instance = AsyncITDog()
            await itdog_instance.initialize()
            logger.info("ITDog 实例已初始化")
        
        if request.name == "itdog_network_test":
            # 获取参数
            url = request.arguments.get("url", "")
            speedtype = request.arguments.get("speedtype", "ipv4web")
            dns = request.arguments.get("dns", "")
            node = request.arguments.get("node", "")
            
            # 验证必需参数
            if not url:
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps({
                                "code": 400,
                                "msg": "URL参数不能为空",
                                "data": None
                            }, ensure_ascii=False, indent=2)
                        )
                    ]
                )
            
            logger.info(f"开始测试: URL={url}, 类型={speedtype}, DNS={dns}, 节点={node}")
            
            # 调用测试方法
            result = await itdog_instance.itdog_speedtest(
                url=url,
                speedtype=speedtype, 
                dns=dns,
                node=node
            )
            
            logger.info(f"测试完成，结果代码: {result.get('code', 'unknown')}")
            
            # 返回结果
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(result, ensure_ascii=False, indent=2)
                    )
                ]
            )
        else:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text", 
                        text=json.dumps({
                            "code": 400,
                            "msg": f"未知的工具名称: {request.name}",
                            "data": None
                        }, ensure_ascii=False, indent=2)
                    )
                ]
            )
            
    except Exception as e:
        logger.error(f"处理工具调用时发生错误: {e}")
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps({
                        "code": 500,
                        "msg": f"服务器内部错误: {str(e)}",
                        "data": None
                    }, ensure_ascii=False, indent=2)
                )
            ]
        )

# FastAPI路由
@app.get("/")
async def root():
    """根路径信息"""
    return {
        "name": "ITDog MCP Server",
        "version": "0.1.0",
        "description": "使用itdog网站进行网络测速测试的MCP服务器",
        "endpoints": {
            "mcp": "/mcp",
            "info": "/info",
            "docs": "/docs"
        }
    }

@app.post("/")
async def root_post():
    """处理POST请求到根路径"""
    return await root()

@app.get("/info")
async def get_info():
    """获取服务器信息"""
    global ipv4_nodes, ipv6_nodes
    
    # 计算总节点数
    ipv4_total = sum(len(nodes) for nodes in ipv4_nodes.values()) if ipv4_nodes else 0
    ipv6_total = sum(len(nodes) for nodes in ipv6_nodes.values()) if ipv6_nodes else 0
    
    return {
        "name": "ITDog MCP Server",
        "version": "0.1.0",
        "description": "使用itdog网站进行网络测速测试的MCP服务器",
        "endpoints": {
            "mcp": "/mcp",
            "info": "/info",
            "docs": "/docs"
        },
        "tools": [
            {
                "name": "itdog_network_test",
                "description": "网络测试工具"
            }
        ],
        "status": "running",
        "nodes_info": {
            "ipv4": {
                "total_nodes": ipv4_total,
                "groups": len(ipv4_nodes),
                "group_names": list(ipv4_nodes.keys()) if ipv4_nodes else []
            },
            "ipv6": {
                "total_nodes": ipv6_total,
                "groups": len(ipv6_nodes),
                "group_names": list(ipv6_nodes.keys()) if ipv6_nodes else []
            }
        }
    }

async def process_mcp_request(data: dict) -> dict:
    """处理MCP请求的核心逻辑"""
    try:
        # 手动验证必需字段
        if not isinstance(data, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": "无效的请求"
                }
            }
        
        method = data.get("method")
        if not method:
            return {
                "jsonrpc": "2.0",
                "id": data.get("id"),
                "error": {
                    "code": -32600,
                    "message": "缺少method字段"
                }
            }
        
        request_id = data.get("id")
        params = data.get("params", {})
        
        logger.info(f"处理MCP方法: {method}, ID: {request_id}")
        
        # 根据请求类型分发处理
        if method == "tools/list":
            list_request = ListToolsRequest(method="tools/list")
            tools = await handle_list_tools(list_request)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [tool.model_dump() for tool in tools]
                }
            }
            
        elif method == "tools/call":
            if not params:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32602,
                        "message": "缺少参数"
                    }
                }
                
            tool_name = params.get("name")
            tool_arguments = params.get("arguments", {})
            
            if not tool_name:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32602,
                        "message": "缺少工具名称"
                    }
                }
            
            call_request = CallToolRequest(
                method="tools/call",
                params={
                    "name": tool_name,
                    "arguments": tool_arguments
                },
                name=tool_name,
                arguments=tool_arguments
            )
            call_result = await handle_call_tool(call_request)
            
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [content.model_dump() for content in call_result.content]
                }
            }
            
        elif method == "initialize":
            client_info = params.get("clientInfo", {})
            protocol_version = params.get("protocolVersion", "2025-08-27")
            capabilities = params.get("capabilities", {})
            
            logger.info(f"客户端信息: {client_info}")
            logger.info(f"协议版本: {protocol_version}")
            logger.info(f"客户端能力: {capabilities}")
            
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-08-27",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "itdog-mcp",
                        "version": "0.1.0",
                        "description": "使用itdog网站进行网络测速测试的MCP服务器"
                    }
                }
            }
            
        elif method == "ping":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {}
            }
            
        elif method == "notifications/initialized":
            logger.info("客户端初始化完成")
            # 通知不需要返回响应
            return None
            
        else:
            logger.warning(f"不支持的方法: {method}")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"方法未找到: {method}"
                }
            }
        
    except Exception as e:
        logger.error(f"处理MCP请求时出错: {e}")
        import traceback
        traceback.print_exc()
        
        return {
            "jsonrpc": "2.0",
            "id": data.get("id") if isinstance(data, dict) else None,
            "error": {
                "code": -32603,
                "message": f"内部错误: {str(e)}"
            }
        }

@app.post("/mcp")
async def handle_mcp_request(request: Request):
    """处理MCP请求 - 使用Request直接解析避免Pydantic验证问题"""
    try:
        # 直接解析JSON，避免Pydantic严格验证
        data = await request.json()
        logger.info(f"收到MCP请求: {data}")
        
        # 使用共享的处理逻辑
        response = await process_mcp_request(data)
        
        # 如果是通知（返回None），则返回空响应
        if response is None:
            return {}
            
        return response
        
    except Exception as e:
        logger.error(f"处理MCP请求时出错: {e}")
        import traceback
        traceback.print_exc()
        
        return {
            "jsonrpc": "2.0",
            "id": data.get("id") if 'data' in locals() else None,
            "error": {
                "code": -32603,
                "message": f"内部错误: {str(e)}"
            }
        }

@app.get("/mcp")
async def handle_mcp_get():
    """MCP端点信息"""
    return {
        "message": "MCP JSON-RPC 端点",
        "description": "请使用POST方法发送JSON-RPC请求",
        "methods": [
            "initialize",
            "tools/list", 
            "tools/call",
            "ping",
            "notifications/initialized"
        ],
        "example": {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": "1"
        }
    }

# 添加直接测试端点（用于测试）
@app.post("/test")
async def direct_network_test(test_request: ITDogTestRequest):
    """直接测试端点（用于调试和测试）"""
    try:
        global itdog_instance
        
        # 初始化itdog实例
        if itdog_instance is None:
            logger.info("正在初始化ITDog实例...")
            itdog_instance = AsyncITDog()
            await itdog_instance.initialize()
            logger.info("ITDog 实例已初始化")
        
        result = await itdog_instance.itdog_speedtest(
            url=test_request.url,
            speedtype=test_request.speedtype,
            dns=test_request.dns,
            node=test_request.node
        )
        
        return result
        
    except Exception as e:
        logger.error(f"直接测试时出错: {e}")
        raise HTTPException(status_code=500, detail=f"测试失败: {str(e)}")

# 添加MCP调试端点
@app.post("/debug/mcp")
async def debug_mcp_request(request: Request):
    """调试MCP请求格式"""
    try:
        data = await request.json()
        return {
            "received_data": data,
            "data_type": str(type(data)),
            "keys": list(data.keys()) if isinstance(data, dict) else "not_dict",
            "method": data.get("method") if isinstance(data, dict) else None,
            "params": data.get("params") if isinstance(data, dict) else None
        }
    except Exception as e:
        return {
            "error": str(e),
            "request_headers": dict(request.headers),
            "content_type": request.headers.get("content-type")
        }

# 添加获取节点列表的请求模型
class GetNodesRequest(BaseModel):
    node_type: str = "ipv4"

async def handle_stdio():
    """处理stdio通信"""
    global itdog_instance, ipv4_nodes, ipv6_nodes
    
    logger.info("启动stdio模式...")
    
    try:
        # 初始化itdog实例
        if itdog_instance is None:
            logger.info("正在初始化ITDog实例...")
            itdog_instance = AsyncITDog()
            await itdog_instance.initialize()
            logger.info("ITDog 实例已初始化")
            
            # 获取节点列表
            if not ipv4_nodes:
                logger.info("正在获取IPv4节点列表...")
                ipv4_result = await itdog_instance.get_traceroute_nodes(node_type="ipv4")
                if ipv4_result.get("code") == 200 and ipv4_result.get("data"):
                    # 新的数据结构处理
                    node_data = ipv4_result.get("data")
                    if isinstance(node_data, dict) and "groups" in node_data:
                        ipv4_nodes = node_data.get("groups", {})
                        total_nodes = node_data.get("total_nodes", 0)
                        logger.info(f"获取到 {total_nodes} 个IPv4节点，分 {len(ipv4_nodes)} 组")
                    else:
                        # 兼容旧格式
                        ipv4_nodes = {"未分类": ipv4_result.get("data", [])}
                        logger.info(f"获取到 {len(ipv4_nodes.get('未分类', []))} 个IPv4节点")
            
            if not ipv6_nodes:
                logger.info("正在获取IPv6节点列表...")
                ipv6_result = await itdog_instance.get_traceroute_nodes(node_type="ipv6")
                if ipv6_result.get("code") == 200 and ipv6_result.get("data"):
                    # 新的数据结构处理
                    node_data = ipv6_result.get("data")
                    if isinstance(node_data, dict) and "groups" in node_data:
                        ipv6_nodes = node_data.get("groups", {})
                        total_nodes = node_data.get("total_nodes", 0)
                        logger.info(f"获取到 {total_nodes} 个IPv6节点，分 {len(ipv6_nodes)} 组")
                    else:
                        # 兼容旧格式
                        ipv6_nodes = {"未分类": ipv6_result.get("data", [])}
                        logger.info(f"获取到 {len(ipv6_nodes.get('未分类', []))} 个IPv6节点")
        
        # 逐行读取stdin
        while True:
            try:
                # 从stdin读取一行
                line = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.readline
                )
                
                if not line:  # EOF
                    logger.info("stdin已关闭，退出stdio模式")
                    break
                
                line = line.strip()
                if not line:  # 空行
                    continue
                
                logger.info(f"收到stdio请求: {line}")
                
                # 解析JSON-RPC请求
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.error(f"JSON解析错误: {e}")
                    response = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32700,
                            "message": f"解析错误: {str(e)}"
                        }
                    }
                    print(json.dumps(response, ensure_ascii=False))
                    sys.stdout.flush()
                    continue
                
                # 处理MCP请求
                response = await process_mcp_request(data)
                
                # 发送响应到stdout
                print(json.dumps(response, ensure_ascii=False))
                sys.stdout.flush()
                
            except Exception as e:
                logger.error(f"处理stdio请求时出错: {e}")
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": f"内部错误: {str(e)}"
                    }
                }
                print(json.dumps(response, ensure_ascii=False))
                sys.stdout.flush()
                
    except KeyboardInterrupt:
        logger.info("收到中断信号，退出stdio模式")
    except Exception as e:
        logger.error(f"stdio模式运行错误: {e}")
    finally:
        await cleanup()

def main():
    """主函数"""
    # 添加命令行参数解析
    parser = argparse.ArgumentParser(description='ITDog MCP Server')
    parser.add_argument(
        '--mode', 
        choices=['http', 'stdio'], 
        default='http',
        help='运行模式: http(默认) 或 stdio'
    )
    parser.add_argument(
        '--host',
        default='0.0.0.0',
        help='HTTP模式下的绑定地址 (默认: 0.0.0.0)'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=8000,
        help='HTTP模式下的端口号 (默认: 8000)'
    )
    
    args = parser.parse_args()
    
    if args.mode == 'stdio':
        # stdio模式
        print("ITDog MCP Server (stdio模式)", file=sys.stderr)
        print("================================", file=sys.stderr)
        print("服务器启动中...", file=sys.stderr)
        print("使用 Ctrl+C 停止服务器", file=sys.stderr)
        print("", file=sys.stderr)
        
        try:
            asyncio.run(handle_stdio())
        except KeyboardInterrupt:
            logger.info("收到键盘中断信号，服务器停止")
        except Exception as e:
            logger.error(f"服务器运行时发生错误: {e}")
    else:
        # HTTP模式
        import uvicorn
        
        print("ITDog MCP Server (HTTP模式)")
        print("===============================")
        print("服务器启动中...")
        print(f"服务地址: http://{args.host}:{args.port}")
        print(f"API文档: http://{args.host}:{args.port}/docs")
        print(f"MCP端点: http://{args.host}:{args.port}/mcp")
        print(f"MCP调试: http://{args.host}:{args.port}/debug/mcp")
        print(f"直接测试: http://{args.host}:{args.port}/test")
        print(f"获取节点: http://{args.host}:{args.port}/nodes")
        print(f"服务器信息: http://{args.host}:{args.port}/info")
        print("使用 Ctrl+C 停止服务器")
        print("")
        
        try:
            uvicorn.run(
                "main:app",
                host=args.host,
                port=args.port,
                log_level="info",
                access_log=True,
                reload=False
            )
        except KeyboardInterrupt:
            logger.info("收到键盘中断信号，服务器停止")
        except Exception as e:
            logger.error(f"服务器运行时发生错误: {e}")

if __name__ == "__main__":
    main()
