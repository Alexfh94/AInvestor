"""Allow running with: python -m ainvestor"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("ainvestor.main:app", host="0.0.0.0", port=8000, reload=True)
