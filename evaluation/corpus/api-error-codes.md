# Acme Cloud — API Error Codes

**ERR_1001.** Error code ERR_1001 means the API key is missing from the request header. Add an Authorization header with a valid key to resolve it.

**ERR_1002.** Error code ERR_1002 means the API key has expired. Generate a new key from the dashboard under Settings > API Keys.

**ERR_2050.** Error code ERR_2050 means the request payload exceeded the 5 MB size limit. Split large uploads into multiple smaller requests.

**ERR_3100.** Error code ERR_3100 means the rate limit of 100 requests per minute was exceeded. Wait 60 seconds before retrying, or contact support to raise your limit.

**ERR_4021.** Error code ERR_4021 means the uploaded file exceeded the maximum size of 10 MB. Compress the file or upload it in parts.
