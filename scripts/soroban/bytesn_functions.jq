{ "sizes": [(.. | .type? // empty) | select(type == "object") | select(.type | endswith("BytesN")) | .params[0].type] | unique }
