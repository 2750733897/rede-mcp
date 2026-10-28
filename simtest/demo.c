/*--------------------------------------------------------------------
 * demo.c - standalone ARM target program used to validate the ReDe MCP
 *          server control loop (breakpoint / step / inspect).
 *
 * Freestanding: no libc, no startup files. Entry point is main().
 * Built with arm-elf-gcc -g -O0 -nostdlib -nostartfiles -Wl,-e,main
 *
 * Marked volatile so the optimiser / simulator cannot elide the loops.
 *------------------------------------------------------------------*/

volatile int g_counter = 0;
volatile int g_array[4] = { 11, 22, 33, 44 };

struct point {
    int x;
    int y;
};

int add(int a, int b)
{
    return a + b;
}

int compute(struct point *p)
{
    int s = 0;
    int i;

    for (i = 0; i < 3; i++) {
        s = add(p->x, i);
        g_counter = s;
    }
    return s;
}

int main(void)
{
    struct point p;
    int r;
    int i;

    p.x = 7;
    p.y = 9;

    r = compute(&p);
    g_array[0] = r;

    for (i = 0; i < 20; i++) {
        g_counter = g_counter + i;
    }

    return r;
}
