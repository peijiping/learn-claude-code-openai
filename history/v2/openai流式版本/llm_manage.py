#!/usr/bin/env python3
"""
llm_manage.py - 大模型管理模块

统一管理大模型的初始化和配置，供其他模块引用。
"""

import os

from dotenv import load_dotenv
from openai import OpenAI


class LLMClient:
    """
    大模型客户端类，用于初始化和管理大模型实例。
    """

    def __init__(self):
        # 加载环境变量
        load_dotenv(override=True)

        
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        self.base_url = os.environ.get("OPENAI_BASE_URL", "")
        self.timeout: int = 1200
        self.max_retries: int = 3

        # 检查是否配置了模型参数
        if  not self.api_key or not self.base_url:
            raise ValueError("请配置 OPENAI_API_KEY、OPENAI_BASE_URL 环境变量")

        # 初始化大模型实例
        self.llm = self.create_llm()

    def create_llm(
        self,
        api_key: str = None,
        base_url: str = None,
        timeout: int = None,
        max_retries: int = None,
        # temperature: float = 0.0,
        # max_tokens: int = 8000,
    ) -> OpenAI:
        """
        创建并返回一个 OpenAI 实例。

        Args:
            model: 模型ID，默认从环境变量读取
            api_key: API密钥，默认从环境变量读取
            base_url: API基础URL，默认从环境变量读取
            timeout: 请求超时时间，默认1200秒
            max_retries: 最大重试次数，默认3次
            temperature: 温度参数，控制输出的随机性
            max_tokens: 最大生成token数

        Returns:
            ChatOpenAI 实例
        """
        return OpenAI(
            api_key=api_key or self.api_key,
            base_url=base_url or self.base_url,
            timeout=timeout or self.timeout,
            max_retries=max_retries or self.max_retries,
        )



# if __name__ == "__main__":
#     llm_client = LLMClient()
#     print(llm_client.llm)