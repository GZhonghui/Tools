#ifndef ENGINE_H
#define ENGINE_H

#include"../common/message.h"

#include"../window/window.h"

#include"../world/world.h"

#include"../control/control.h"

class Game
{
protected:
    Window *game_window;
    World *game_world;

public:
    Game():
    game_window(nullptr),
    game_world(nullptr){}

public:
    ~Game()
    {
        this->close();
    }

protected:
    void set_event();

public:
    void init();
    void run();
    void close();
};

#endif