class DynamicArray:
    def __init__(self): 
        self.capacity = 2
        self.length = 0
        self.arr = [0] * 2

    def pushback(self, n): 
        if self.length == self.capacity: 
            self.resize()
        self.arr[self.length] = n
        self.length += 1

    def resize(self):
        self.capacity = 2 * self.capacity
        new_arr = [0] * self.capacity
        for i in range(self.length):
            new_arr[i] = self.arr[i]
        self.arr = new_arr
    
    def popback(self):
        if self.length > 0: 
            self.length -= 1

    def get(self, i): 
        if i < self.length: 
            return self.arr[i]
        #return -1
        raise IndexError("Index out of bounds")
    
    def insert(self, i, n): 
        if i < self.length:
            self.arr[i] = n

    def print(self): 
        # s = ""
        # for i in range(self.length):
        #     s += str(self.arr[i]) + " "
        # print(s)
        result = []
        for i in range(self.length):
            result.append(str(self.arr[i]))
        print(" ".join(result))

    