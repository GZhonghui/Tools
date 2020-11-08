#ifndef ENGINE_H
#define ENGINE_H

#include"../window/window.h"

class Game
{
protected:
    Window *game_window;

public:
    Game():game_window(nullptr){}

public:
    ~Game()
    {
        this->close();
    }

public:
    void init();
    void run();
    void close();
};

#endif