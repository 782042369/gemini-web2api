"""Validate consumed API fields before any uploads or upstream requests.

Unknown optional fields remain accepted for SDK forward compatibility. Known
fields must have the shapes the prompt converters consume; validation errors
carry field paths, not the user's prompt or credentials.
"""
import base64
import binascii
from urllib.parse import urlsplit


class RequestValidationError(ValueError):
    """A client input error with a precise field path."""

    def __init__(self, param, expected):
        """Build a safe error. Args: param is a field path, expected a type. Returns: None."""
        self.param = param
        super().__init__(f"{param} {expected}")


def _object(value, param):
    """Require an object. Args: value, param (field path). Returns: the object."""
    if not isinstance(value, dict):
        raise RequestValidationError(param, "must be an object")
    return value


def _array(value, param, nonempty=False):
    """Require an array. Args: value, param, nonempty. Returns: the array."""
    if not isinstance(value, list) or (nonempty and not value):
        raise RequestValidationError(param, "must be a non-empty array" if nonempty else "must be an array")
    return value


def _string(value, param, nonempty=False):
    """Require text. Args: value, param, nonempty. Returns: the string."""
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise RequestValidationError(param, "must be a non-empty string" if nonempty else "must be a string")
    return value


def _optional_text(obj, key, param):
    """Check optional text. Args: obj, key, param prefix. Returns: None."""
    if obj.get(key) is not None:
        _string(obj[key], f"{param}.{key}")


def _function(function, param, declaration=False):
    """Check a function. Args: function object, param, declaration flag. Returns: None."""
    _object(function, param)
    _string(function.get("name"), f"{param}.name", nonempty=True)
    if declaration:
        _optional_text(function, "description", param)
        for key in ("parameters", "parametersJsonSchema"):
            if key in function:
                _object(function[key], f"{param}.{key}")
    elif "arguments" in function and not isinstance(function["arguments"], (str, dict)):
        raise RequestValidationError(f"{param}.arguments", "must be a JSON string or object")


def _image_part(part, param):
    """Check image metadata shapes. Args: part, param prefix. Returns: None."""
    for key in ("mime_type", "media_type", "data", "base64", "url", "file_id"):
        _optional_text(part, key, param)
    if "image_url" in part:
        image_url = part["image_url"]
        if isinstance(image_url, dict):
            _string(image_url.get("url"), f"{param}.image_url.url", nonempty=True)
            _optional_text(image_url, "mime_type", f"{param}.image_url")
        else:
            _string(image_url, f"{param}.image_url", nonempty=True)
    from .tools import _image_from_part
    image = _image_from_part(part)
    if not image or not image[0]:
        raise RequestValidationError(param, "must provide valid image data or an HTTP(S) URL")
    if isinstance(image[0], str):
        try:
            parsed = urlsplit(image[0])
            valid_url = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        except ValueError:
            valid_url = False
        if not valid_url:
            raise RequestValidationError(param, "must use an HTTP(S) image URL")


def _openai_content(content, param):
    """Check content parts. Args: content string/list/null, param. Returns: None."""
    if content is None or isinstance(content, str):
        return
    for index, part in enumerate(_array(content, param)):
        prefix = f"{param}[{index}]"
        _object(part, prefix)
        kind = _string(part.get("type"), f"{prefix}.type", nonempty=True)
        if kind in ("text", "input_text", "output_text"):
            _string(part.get("text"), f"{prefix}.text")
        elif kind in ("image", "image_url", "input_image"):
            _image_part(part, prefix)
        elif kind == "function_call":
            _function(part, prefix)


def _message(message, param):
    """Check a message and its calls. Args: message, param. Returns: None."""
    _object(message, param)
    _string(message.get("role", "user"), f"{param}.role", nonempty=True)
    _openai_content(message.get("content"), f"{param}.content")
    for key in ("name", "tool_call_id"):
        _optional_text(message, key, param)
    if message.get("tool_calls") is not None:
        for index, call in enumerate(_array(message["tool_calls"], f"{param}.tool_calls")):
            prefix = f"{param}.tool_calls[{index}]"
            _object(call, prefix)
            _function(call.get("function"), f"{prefix}.function")


def _openai_tools(req):
    """Check declared functions and choices. Args: req object. Returns: None."""
    tools = req.get("tools")
    names = set()
    if tools is not None:
        for index, tool in enumerate(_array(tools, "tools")):
            prefix = f"tools[{index}]"
            _object(tool, prefix)
            if tool.get("type", "function") != "function":
                raise RequestValidationError(f"{prefix}.type", "is unsupported; only function tools are available")
            fn = tool.get("function", tool)
            _function(fn, prefix + (".function" if "function" in tool else ""), declaration=True)
            if fn["name"] in names:
                raise RequestValidationError(f"{prefix}.name", "must be unique")
            names.add(fn["name"])
    choice = req.get("tool_choice", "auto")
    if choice is None:
        return
    if isinstance(choice, str) and choice in ("auto", "none", "required"):
        if choice == "required" and not names:
            raise RequestValidationError("tool_choice", "requires at least one declared function")
        return
    _object(choice, "tool_choice")
    if choice.get("type") != "function":
        raise RequestValidationError("tool_choice.type", "must be function")
    fn = choice.get("function", choice)
    _function(fn, "tool_choice")
    if fn["name"] not in names:
        raise RequestValidationError("tool_choice.name", "must name a declared function")


def _openai_common(req):
    """Check common options. Args: req object. Returns: None."""
    if "model" in req:
        _string(req["model"], "model", nonempty=True)
    if "stream" in req and not isinstance(req["stream"], bool):
        raise RequestValidationError("stream", "must be a boolean")
    options = req.get("stream_options")
    if options is not None:
        _object(options, "stream_options")
        if "include_usage" in options and not isinstance(options["include_usage"], bool):
            raise RequestValidationError("stream_options.include_usage", "must be a boolean")
    _openai_tools(req)


def validate_chat_request(req):
    """Validate Chat input before processing. Args: req object. Returns: None; raises on invalid fields."""
    _openai_common(req)
    for index, message in enumerate(_array(req.get("messages"), "messages", nonempty=True)):
        _message(message, f"messages[{index}]")


def validate_responses_request(req):
    """Validate Responses input before normalization. Args: req object. Returns: None; raises on invalid fields."""
    _openai_common(req)
    if req.get("instructions") is not None:
        _string(req["instructions"], "instructions")
    value = req.get("input", [])
    if isinstance(value, str):
        return
    for index, item in enumerate(_array(value, "input")):
        if isinstance(item, str):
            continue
        prefix = f"input[{index}]"
        _object(item, prefix)
        kind = item.get("type", "message")
        _string(kind, f"{prefix}.type")
        if kind == "function_call":
            _function(item, prefix)
            _string(item.get("call_id"), f"{prefix}.call_id", nonempty=True)
        elif kind == "function_call_output":
            _string(item.get("call_id"), f"{prefix}.call_id", nonempty=True)
            _openai_content(item.get("output"), f"{prefix}.output")
            _optional_text(item, "name", prefix)
        elif kind in ("input_text", "input_image", "image"):
            _openai_content([item], prefix)
        else:
            _message(item, prefix)


def _google_content(content, param):
    """Check a Google Content object. Args: content, param. Returns: None."""
    _object(content, param)
    _optional_text(content, "role", param)
    for index, part in enumerate(_array(content.get("parts", []), f"{param}.parts")):
        prefix = f"{param}.parts[{index}]"
        _object(part, prefix)
        if "text" in part:
            _string(part["text"], f"{prefix}.text")
        if "inlineData" in part:
            data = _object(part["inlineData"], f"{prefix}.inlineData")
            _string(data.get("data"), f"{prefix}.inlineData.data")
            try:
                decoded = base64.b64decode(data["data"], validate=True)
            except (ValueError, binascii.Error):
                decoded = b""
            if not decoded:
                raise RequestValidationError(f"{prefix}.inlineData.data", "must contain valid non-empty base64")
            _optional_text(data, "mimeType", f"{prefix}.inlineData")
        for key, args_key in (("functionCall", "args"), ("functionResponse", "response")):
            if key in part:
                value = _object(part[key], f"{prefix}.{key}")
                _string(value.get("name"), f"{prefix}.{key}.name", nonempty=True)
                if args_key in value:
                    _object(value[args_key], f"{prefix}.{key}.{args_key}")


def validate_google_request(req):
    """Validate Google structures before batching. Args: req object. Returns: None; raises on invalid fields."""
    for index, content in enumerate(_array(req.get("contents"), "contents", nonempty=True)):
        _google_content(content, f"contents[{index}]")
    if req.get("systemInstruction") is not None:
        _google_content(req["systemInstruction"], "systemInstruction")
    names = set()
    if req.get("tools") is not None:
        for index, group in enumerate(_array(req["tools"], "tools")):
            prefix = f"tools[{index}]"
            _object(group, prefix)
            for fn_index, fn in enumerate(_array(group.get("functionDeclarations", []), f"{prefix}.functionDeclarations")):
                _function(fn, f"{prefix}.functionDeclarations[{fn_index}]", declaration=True)
                names.add(fn["name"])
    config = req.get("toolConfig")
    if config is None:
        return
    _object(config, "toolConfig")
    calling = config.get("functionCallingConfig")
    if calling is None:
        return
    _object(calling, "toolConfig.functionCallingConfig")
    mode = calling.get("mode", "AUTO")
    if not isinstance(mode, str) or mode not in ("AUTO", "NONE", "ANY", "VALIDATED"):
        raise RequestValidationError("toolConfig.functionCallingConfig.mode", "must be AUTO, NONE, ANY, or VALIDATED")
    if calling.get("allowedFunctionNames") is not None:
        for name in _array(calling["allowedFunctionNames"], "toolConfig.functionCallingConfig.allowedFunctionNames"):
            _string(name, "toolConfig.functionCallingConfig.allowedFunctionNames[]", nonempty=True)
            if name not in names:
                raise RequestValidationError("toolConfig.functionCallingConfig.allowedFunctionNames", "must name declared functions")
