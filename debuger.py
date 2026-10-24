a = 2

for i in range(10):
    if i == 5:
        breakpoint()

    print(f"i={i}, a={a}")
    a += 2
    a *= 2

print(f"Final value of a: {a}")
