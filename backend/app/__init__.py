from fastapi import FastAPI




def create_app(lifespan) -> FastAPI:
    from .routers import note, provider, model, config, chat, library, collection
    app = FastAPI(title="BiliNote",lifespan=lifespan)
    app.include_router(note.router, prefix="/api")
    app.include_router(provider.router, prefix="/api")
    app.include_router(model.router,prefix="/api")
    app.include_router(config.router,  prefix="/api")
    app.include_router(chat.router, prefix="/api")
    app.include_router(library.router, prefix="/api")

    app.include_router(collection.router, prefix="/api")

    return app
