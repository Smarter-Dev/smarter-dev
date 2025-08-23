"""Centralized LLM configuration for the project.

This module provides a consistent way to configure LLM models across the entire
project using environment variables.
"""

import os
import dotenv
import dspy
from typing import Optional


def get_llm_model(model_type: str = "default") -> dspy.LM:
    """Get configured LLM model based on environment variables.
    
    Args:
        model_type: Type of model to get ("default", "judge", etc.)
        
    Returns:
        Configured dspy.LM instance
        
    Environment Variables:
        LLM_MODEL: Main model for the project (default: gemini/gemini-2.0-flash-lite)
        LLM_JUDGE_MODEL: Model for LLM-as-judge tests (default: gemini/gemini-2.5-flash-lite)
        OPENAI_API_KEY: OpenAI API key
        GEMINI_API_KEY: Google Gemini API key
        
    Examples:
        # Use GPT-5 Nano for main model
        LLM_MODEL=gpt-5-nano-2025-08-07 python -m smarter_dev.bot.client
        
        # Use GPT-5 Nano for judge tests
        LLM_JUDGE_MODEL=gpt-5-nano-2025-08-07 python test_mention_agent.py --llm-only
    """
    # Get model based on type
    if model_type == "judge":
        model_name = os.getenv("LLM_JUDGE_MODEL", "gemini/gemini-2.5-flash-lite")
    else:
        model_name = os.getenv("LLM_MODEL", "gemini/gemini-2.0-flash-lite")
    
    # Determine API key based on model provider
    api_key = _get_api_key_for_model(model_name)
    
    # Special handling for cache setting
    cache = False if model_type == "default" else True
    
    # Special parameters for reasoning models
    kwargs = {
        "api_key": api_key,
        "cache": cache
    }
    
    # Ensure OpenAI models use the correct provider format
    provider = _get_provider_from_model(model_name)
    if provider == "openai" and not model_name.startswith("openai/"):
        # DSPy might need explicit provider prefix for OpenAI models
        formatted_model_name = f"openai/{model_name}"
    else:
        formatted_model_name = model_name
    
    # OpenAI reasoning models (o1, GPT-5) require specific parameters
    if _is_reasoning_model(model_name):
        kwargs["temperature"] = 1.0
        kwargs["max_tokens"] = 25000  # High limit for reasoning models
    
    return dspy.LM(formatted_model_name, **kwargs)


def get_model_info(model_type: str = "default") -> dict:
    """Get information about the configured model.
    
    Args:
        model_type: Type of model to get info for
        
    Returns:
        Dict with model_name, provider, and has_api_key info
    """
    if model_type == "judge":
        model_name = os.getenv("LLM_JUDGE_MODEL", "gemini/gemini-2.5-flash-lite")
    else:
        model_name = os.getenv("LLM_MODEL", "gemini/gemini-2.0-flash-lite")
    
    provider = _get_provider_from_model(model_name)
    api_key = _get_api_key_for_model(model_name)
    
    return {
        "model_name": model_name,
        "provider": provider,
        "has_api_key": bool(api_key),
        "env_var": "LLM_JUDGE_MODEL" if model_type == "judge" else "LLM_MODEL"
    }


def _get_provider_from_model(model_name: str) -> str:
    """Determine provider from model name."""
    if model_name.startswith("gpt-") or model_name.startswith("openai/"):
        return "openai"
    elif model_name.startswith("gemini/"):
        return "gemini"
    elif model_name.startswith("claude-"):
        return "anthropic"
    else:
        return "unknown"


def _get_api_key_for_model(model_name: str) -> Optional[str]:
    """Get appropriate API key for model."""
    provider = _get_provider_from_model(model_name)
    
    if provider == "openai":
        return dotenv.get_key(".env", "OPENAI_API_KEY")
    elif provider == "gemini":
        return dotenv.get_key(".env", "GEMINI_API_KEY")
    elif provider == "anthropic":
        return dotenv.get_key(".env", "ANTHROPIC_API_KEY")
    else:
        # Fallback - try common keys
        return (dotenv.get_key(".env", "OPENAI_API_KEY") or 
                dotenv.get_key(".env", "GEMINI_API_KEY") or
                dotenv.get_key(".env", "ANTHROPIC_API_KEY"))


def _is_reasoning_model(model_name: str) -> bool:
    """Check if model is a reasoning model requiring special parameters."""
    reasoning_models = [
        "o1-preview", "o1-mini", "o1",
        "gpt-5-nano", "gpt-5-mini", "gpt-5",
        "openai/o1-preview", "openai/o1-mini", "openai/o1",
        "openai/gpt-5-nano", "openai/gpt-5-mini", "openai/gpt-5"
    ]
    
    # Check exact matches and prefix matches for dated models
    for reasoning_model in reasoning_models:
        if model_name == reasoning_model or model_name.startswith(f"{reasoning_model}-"):
            return True
    
    return False


def validate_model_config(model_type: str = "default") -> tuple[bool, str]:
    """Validate that model configuration is complete.
    
    Args:
        model_type: Type of model to validate
        
    Returns:
        (is_valid, error_message) tuple
    """
    try:
        info = get_model_info(model_type)
        
        if not info["has_api_key"]:
            provider_key = f"{info['provider'].upper()}_API_KEY"
            return False, f"Missing {provider_key} in .env for model {info['model_name']}"
        
        return True, ""
    except Exception as e:
        return False, f"Configuration error: {e}"