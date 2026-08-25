[
  .[] | select(
    ( [(.fields? // [])[]           | .recursive] +
      [(.variants? // [])[] | (.fields? // [])[] | .recursive] )
    | any | not
  )
]