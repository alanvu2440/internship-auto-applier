class StaticArray: 
    def insertEnd(self, arr, n, length, capacity):
        if length < capacity:
            arr[length] = n
    
    def removeEnd(self, arr, length):
        if length > 0: 
            arr[length - 1] = 0

    def insertMiddle(self, arr, i, n, length):
        for index in range(length - 1, i - 1, -1):
            arr[index + 1] = arr[index]
        arr[i] = n;

    def removeMiddle(self, arr, i, length):
        for index in range(i + 1, length, +1):
            arr[index - 1] = arr[index]

    def printArr(self, arr, length):
        s =""
        for i in range(length):
            s += str(arr[i]) + " "
        print(s)

