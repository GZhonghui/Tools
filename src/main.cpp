#include"engine/engine.h"

Game *main_game(nullptr);

int main()
{
    main_game=new Game();

    main_game->init();
    main_game->run();
    main_game->close();

    delete main_game;

    return 0;
}