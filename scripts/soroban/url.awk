BEGIN {
    FS = "="
    origin = 0
}

/^[[]remote/ {
    origin = 1
    next
}

/^[[]/ {
    origin = 0
    next
}

/url/ && origin { print $2; }
