#ifndef TEXTURE_H
#define TEXTURE_H

class Texture
{
protected:
    unsigned char *image;

public:
    Texture():image(nullptr){}

public:
    ~Texture()=default;
};

#endif