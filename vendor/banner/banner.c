/* banner — bundled demo package for fix: prints text in big ASCII letters.
 * A deliberately tiny figlet replacement with a built-in 5-row font.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

typedef struct { char ch; const char *rows[5]; } Glyph;

static const Glyph FONT[] = {
{'A', {"###","# #","###","# #","# #"}},
{'B', {"## ","# #","## ","# #","## "}},
{'C', {"###","#  ","#  ","#  ","###"}},
{'D', {"## ","# #","# #","# #","## "}},
{'E', {"###","#  ","## ","#  ","###"}},
{'F', {"###","#  ","## ","#  ","#  "}},
{'G', {"###","#  ","# #","# #","###"}},
{'H', {"# #","# #","###","# #","# #"}},
{'I', {"###"," # "," # "," # ","###"}},
{'J', {"###","  #","  #","# #","###"}},
{'K', {"# #","# #","## ","# #","# #"}},
{'L', {"#  ","#  ","#  ","#  ","###"}},
{'M', {"#   #","## ##","# # #","#   #","#   #"}},
{'N', {"#   #","##  #","# # #","#  ##","#   #"}},
{'O', {"###","# #","# #","# #","###"}},
{'P', {"###","# #","###","#  ","#  "}},
{'Q', {"###","# #","###","  #","  ##"}},
{'R', {"###","# #","## ","# #","# #"}},
{'S', {"###","#  ","###","  #","###"}},
{'T', {"###"," # "," # "," # "," # "}},
{'U', {"# #","# #","# #","# #","###"}},
{'V', {"# #","# #","# #","# #"," # "}},
{'W', {"#   #","#   #","# # #","## ##","#   #"}},
{'X', {"# #","# #"," # ","# #","# #"}},
{'Y', {"# #","# #"," # "," # "," # "}},
{'Z', {"###","  #"," # ","#  ","###"}},
{'0', {"###","# #","# #","# #","###"}},
{'1', {" # ","## "," # "," # ","###"}},
{'2', {"###","  #"," # ","#  ","###"}},
{'3', {"###","  #","###","  #","###"}},
{'4', {"# #","# #","###","  #","  #"}},
{'5', {"###","#  ","###","  #","###"}},
{'6', {"###","#  ","###","# #","###"}},
{'7', {"###","  #"," # "," # "," # "}},
{'8', {"###","# #","###","# #","###"}},
{'9', {"###","# #","###","  #","###"}},
{' ', {"  ","  ","  ","  ","  "}},
{'-', {"   ","   ","###","   ","   "}},
{'.', {"  ","  ","  ","  ","# "}},
{'!', {" # "," # "," # ","   "," # "}},
{'?', {"###","  #"," ##","   "," # "}},
{0,   {"","","","",""}}
};

static const Glyph *find_glyph(char c)
{
    const Glyph *g;
    for (g = FONT; g->ch; g++)
        if (g->ch == c)
            return g;
    return NULL;
}

static int row_width(const Glyph *g, int row)
{
    return (int)strlen(g->rows[row]);
}

int main(int argc, char **argv)
{
    char text[256];
    int i, row;

    if (argc > 1) {
        size_t len = 0;
        for (i = 1; i < argc && len < sizeof(text) - 1; i++) {
            size_t n = strlen(argv[i]);
            if (len + n + 2 >= sizeof(text))
                break;
            if (i > 1)
                text[len++] = ' ';
            memcpy(text + len, argv[i], n);
            len += n;
        }
        text[len] = '\0';
    } else {
        if (!fgets(text, sizeof(text), stdin))
            return 1;
        text[strcspn(text, "\n")] = '\0';
    }

    for (i = 0; text[i]; i++)
        text[i] = (char)toupper((unsigned char)text[i]);

    for (row = 0; row < 5; row++) {
        for (i = 0; text[i]; i++) {
            const Glyph *g = find_glyph(text[i]);
            const char *unknown = "   ";
            const char *s = g ? g->rows[row] : unknown;
            int w = g ? row_width(g, row) : 3;
            int pad = (g ? (int)strlen(g->rows[0]) : 3) - w;
            printf("%s", s);
            while (pad-- > 0)
                putchar(' ');
            if (text[i + 1])
                putchar(' ');
        }
        putchar('\n');
    }
    return 0;
}
