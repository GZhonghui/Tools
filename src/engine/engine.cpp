#include"engine.h"

void Game::init()
{

}

void Game::run()
{

}

void Game::close()
{
    if(game_window)
    {
        delete game_window;
        game_window=nullptr;
    }
}