#ifndef MESSAGE_H
#define MESSAGE_H

#include<unordered_set>
#include<algorithm>
#include<cstdio>
#include<ctime>

const int message_size=128;

enum MessageType
{
    MESSAGE,
    WARNING,
    ERROR
};

class Message
{
public:
    Message()=default;

public:
    ~Message()=default;

public:
    static void print(MessageType message_type,const char *message);
    static void print_once(unsigned int id,const char *message);
    static void print_bar(int now,bool last=false,int width=52);
};

#endif
