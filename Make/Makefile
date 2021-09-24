all: Project

CC = g++

Target = Engine

InclDir = -I Support/Include

LinkDir = Support/Lib

CompFlag = -Wall -O2 -c --std=c++17 $(InclDir)
LinkFlag = -l pthread

Objects  = Main.o
Objects += SupportA.o
Objects += SupportB.o

%.o: Project/%.cpp; -@ $(CC) -o $@ $< $(CompFlag)

Project: $(Objects); -@ $(CC) $(LinkFlag) $(Objects) -o $(Target)

Auto  = @set -e; rm -f $@;
Auto += $(CC) -MM $(InclDir) $< > $@.$$$$;
Auto += sed 's,\($*\)\.o[ :]*,\1.o $@ : ,g' < $@.$$$$ > $@;
Auto += rm -f $@.$$$$

%.d: Project/%.cpp; $(Auto)

-include $(Objects:.o=.d)

clean: ; -@ rm $(Target) $(Objects) $(Objects:.o=.d)