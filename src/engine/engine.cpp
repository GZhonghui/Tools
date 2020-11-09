#include"engine.h"

void Game::set_event()
{
    Control::key_down;
}

void Game::init()
{
    game_window=new Window();
    Message::print(MessageType::MESSAGE,"Init window.");

    game_world=new World();
    Message::print(MessageType::MESSAGE,"Init world.");
}

void Game::run()
{
    if(game_window)
    {
        game_window->show();
    }
}

void Game::close()
{
    if(game_window)
    {
        delete game_window;
        game_window=nullptr;
    }
}