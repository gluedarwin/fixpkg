/* hello — bundled demo package for fix.
 * Build: cc -O2 -Wall -o hello hello.c
 */
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
    const char *who = "fix";
    if (argc > 1 && argv[1][0] != '\0')
        who = argv[1];
    printf("hello, %s! (built from source by fix)\n", who);
    return 0;
}
