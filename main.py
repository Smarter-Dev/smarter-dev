from starlette.applications import Starlette
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.requests import Request
import uvicorn

templates = Jinja2Templates(directory="templates")

async def homepage(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

async def discord_redirect(request: Request):
    return RedirectResponse(url="https://discord.gg/de8kajxbYS", status_code=302)

async def not_found(request: Request, exc: HTTPException):
    return templates.TemplateResponse("404.html", {"request": request}, status_code=404)

async def server_error(request: Request, exc: HTTPException):
    return templates.TemplateResponse("500.html", {"request": request}, status_code=500)

routes = [
    Route("/", homepage),
    Route("/discord", discord_redirect),
]

exception_handlers = {
    404: not_found,
    500: server_error,
}

app = Starlette(
    routes=routes,
    exception_handlers=exception_handlers,
)

app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
